import torch
import torch.nn as nn
import torch.nn.functional as F
import fvcore.nn.weight_init as weight_init
import numbers
from torch.nn import Softmax
import math
import torch.utils.checkpoint as checkpoint
from functools import partial
from typing import Optional, Callable
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
from mamba_ssm.ops.selective_scan_interface import selective_scan_fn, selective_scan_ref
from einops import rearrange, repeat
from thop import profile
from torch.nn import init as init
from pdb import set_trace as stx
from pytorch_wavelets import DWTForward, DWTInverse  # (or import DWT, IDWT)
import time
from torch.autograd import Function
from torch.autograd import Variable, gradcheck
import pywt

# from model_archs.TTST_arc import Attention as TSA
# from model_archs.layer import *
# from model_archs.comm import *
# from model_archs.uvmb import UVMB
NEG_INF = -1000000

device_id0 = 'cuda:0'
device_id1 = 'cuda:1'
device_id2 = 'cuda:2'

def to_3d(x):
    return rearrange(x, 'b c h w -> b (h w) c')

def to_4d(x,h,w):
    return rearrange(x, 'b (h w) c -> b c h w',h=h,w=w)

class BiasFree_LayerNorm(nn.Module):
    def __init__(self, normalized_shape):
        super(BiasFree_LayerNorm, self).__init__()
        if isinstance(normalized_shape, numbers.Integral):
            normalized_shape = (normalized_shape,)
        normalized_shape = torch.Size(normalized_shape)

        assert len(normalized_shape) == 1

        self.weight = nn.Parameter(torch.ones(normalized_shape))
        self.normalized_shape = normalized_shape

    def forward(self, x):
        sigma = x.var(-1, keepdim=True, unbiased=False)
        return x / torch.sqrt(sigma + 1e-5) * self.weight


class WithBias_LayerNorm(nn.Module):
    def __init__(self, normalized_shape):
        super(WithBias_LayerNorm, self).__init__()
        if isinstance(normalized_shape, numbers.Integral):
            normalized_shape = (normalized_shape,)
        normalized_shape = torch.Size(normalized_shape)

        assert len(normalized_shape) == 1

        self.weight = nn.Parameter(torch.ones(normalized_shape))
        self.bias = nn.Parameter(torch.zeros(normalized_shape))
        self.normalized_shape = normalized_shape

    def forward(self, x):
        mu = x.mean(-1, keepdim=True)
        sigma = x.var(-1, keepdim=True, unbiased=False)
        return (x - mu) / torch.sqrt(sigma + 1e-5) * self.weight + self.bias

    def initialize(self):
        weight_init(self)


class LayerNorm(nn.Module):
    def __init__(self, dim, LayerNorm_type):
        super(LayerNorm, self).__init__()
        if LayerNorm_type == 'BiasFree':
            self.body = BiasFree_LayerNorm(dim)
        else:
            self.body = WithBias_LayerNorm(dim)

    def forward(self, x):
        h, w = x.shape[-2:]
        return to_4d(self.body(to_3d(x)), h, w)

    def initialize(self):
        weight_init(self)


class FeedForward(nn.Module):
    def __init__(self, dim, ffn_expansion_factor, bias):
        super(FeedForward, self).__init__()

        self.dwconv1 = nn.Conv2d(dim, dim, kernel_size=3, stride=1, padding=1,groups=dim, bias=bias)
        self.dwconv2 = nn.Conv2d(dim*2, dim*2, kernel_size=3, stride=1, padding=1, groups=dim, bias=bias)
        self.project_out = nn.Conv2d(dim*4, dim, kernel_size=1, bias=bias)
        self.weight = nn.Sequential(
            nn.Conv2d(dim, dim // 16, 1, bias=True),
            nn.BatchNorm2d(dim // 16),
            nn.ReLU(True),
            nn.Conv2d(dim // 16, dim, 1, bias=True),
            nn.Sigmoid())
        self.weight1 = nn.Sequential(
            nn.Conv2d(dim*2, dim // 16, 1, bias=True),
            nn.BatchNorm2d(dim // 16),
            nn.ReLU(True),
            nn.Conv2d(dim // 16, dim*2, 1, bias=True),
            nn.Sigmoid())
    def forward(self, x):

        x_f = torch.abs(self.weight(torch.fft.fft2(x.float()).real)*torch.fft.fft2(x.float()))
        x_f_gelu = F.gelu(x_f) * x_f

        x_s   = self.dwconv1(x)
        x_s_gelu = F.gelu(x_s) * x_s

        x_f = torch.fft.fft2(torch.cat((x_f_gelu,x_s_gelu),1))
        x_f = torch.abs(torch.fft.ifft2(self.weight1(x_f.real) * x_f))

        x_s = self.dwconv2(torch.cat((x_f_gelu,x_s_gelu),1))
        out = self.project_out(torch.cat((x_f,x_s),1))

        return out

    def initialize(self):
        weight_init(self)

def custom_complex_normalization(input_tensor, dim=-1):
    real_part = input_tensor.real
    imag_part = input_tensor.imag
    norm_real = F.softmax(real_part, dim=dim)
    norm_imag = F.softmax(imag_part, dim=dim)

    normalized_tensor = torch.complex(norm_real, norm_imag)

    return normalized_tensor


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class LinearAttention_B(nn.Module):
    r""" Linear Attention with LePE and RoPE.

    Args:
        dim (int): Number of input channels.
        num_heads (int): Number of attention heads.
        qkv_bias (bool, optional):  If True, add a learnable bias to query, key, value. Default: True
    """

    def __init__(self, dim, input_resolution, num_heads, qkv_bias=True, **kwargs):

        super().__init__()
        self.dim = dim
        self.input_resolution = input_resolution
        self.num_heads = num_heads
        self.qk = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.elu = nn.ELU()
        self.lepe = nn.Conv2d(dim, dim, 3, padding=1, groups=dim)
        self.rope = RoPE(shape=(input_resolution[0], input_resolution[1], dim))

    def forward(self, x):
        """
        Args:
            x: input features with shape of (B, N, C)
        """
        b, n, c = x.shape
        h = int(n ** 0.5)
        w = int(n ** 0.5)
        num_heads = self.num_heads
        head_dim = c // num_heads

        qk = self.qk(x).reshape(b, n, 2, c).permute(2, 0, 1, 3)
        q, k, v = qk[0], qk[1], x
        # q, k, v: b, n, c

        q = self.elu(q) + 1.0
        k = self.elu(k) + 1.0
        q_rope = self.rope(q.reshape(b, h, w, c)).reshape(b, n, num_heads, head_dim).permute(0, 2, 1, 3)
        k_rope = self.rope(k.reshape(b, h, w, c)).reshape(b, n, num_heads, head_dim).permute(0, 2, 1, 3)
        q = q.reshape(b, n, num_heads, head_dim).permute(0, 2, 1, 3)
        k = k.reshape(b, n, num_heads, head_dim).permute(0, 2, 1, 3)
        v = v.reshape(b, n, num_heads, head_dim).permute(0, 2, 1, 3)

        z = 1 / (q @ k.mean(dim=-2, keepdim=True).transpose(-2, -1) + 1e-6)
        kv = (k_rope.transpose(-2, -1) * (n ** -0.5)) @ (v * (n ** -0.5))
        x = q_rope @ kv * z

        x = x.transpose(1, 2).reshape(b, n, c)
        v = v.transpose(1, 2).reshape(b, h, w, c).permute(0, 3, 1, 2)
        x = x + self.lepe(v).permute(0, 2, 3, 1).reshape(b, n, c)

        return x

    def extra_repr(self) -> str:
        return f'dim={self.dim}, num_heads={self.num_heads}'





class MLLABlock(nn.Module):
    r""" MLLA Block.

    Args:
        dim (int): Number of input channels.
        input_resolution (tuple[int]): Input resulotion.
        num_heads (int): Number of attention heads.
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
        qkv_bias (bool, optional): If True, add a learnable bias to query, key, value. Default: True
        drop (float, optional): Dropout rate. Default: 0.0
        drop_path (float, optional): Stochastic depth rate. Default: 0.0
        act_layer (nn.Module, optional): Activation layer. Default: nn.GELU
        norm_layer (nn.Module, optional): Normalization layer.  Default: nn.LayerNorm
    """

    def __init__(self, dim, input_resolution, num_heads, mlp_ratio=4., qkv_bias=True, drop=0., drop_path=0.,
                 act_layer=nn.GELU, norm_layer=nn.LayerNorm, **kwargs):
        super().__init__()
        self.dim = dim
        self.input_resolution = input_resolution
        self.num_heads = num_heads
        self.mlp_ratio = mlp_ratio

        self.cpe1 = nn.Conv2d(dim, dim, 3, padding=1, groups=dim)
        self.norm1 = norm_layer(dim)
        self.in_proj = nn.Linear(dim, dim)
        self.act_proj = nn.Linear(dim, dim)
        self.dwc = nn.Conv2d(dim, dim, 3, padding=1, groups=dim)
        self.act = nn.SiLU()
        self.attn = LinearAttention(dim=dim, input_resolution=input_resolution, num_heads=num_heads, qkv_bias=qkv_bias)
        self.out_proj = nn.Linear(dim, dim)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()

        self.cpe2 = nn.Conv2d(dim, dim, 3, padding=1, groups=dim)
        self.norm2 = norm_layer(dim)
        self.mlp = Mlp(in_features=dim, hidden_features=int(dim * mlp_ratio), act_layer=act_layer, drop=drop)

    def forward(self, x):
        H, W = self.input_resolution
        B, L, C = x.shape
        assert L == H * W, "input feature has wrong size"

        x = x + self.cpe1(x.reshape(B, H, W, C).permute(0, 3, 1, 2)).flatten(2).permute(0, 2, 1)
        shortcut = x

        x = self.norm1(x)
        act_res = self.act(self.act_proj(x))
        x = self.in_proj(x).view(B, H, W, C)
        x = self.act(self.dwc(x.permute(0, 3, 1, 2))).permute(0, 2, 3, 1).view(B, L, C)

        # Linear Attention
        x = self.attn(x)

        x = self.out_proj(x * act_res)
        x = shortcut + self.drop_path(x)
        x = x + self.cpe2(x.reshape(B, H, W, C).permute(0, 3, 1, 2)).flatten(2).permute(0, 2, 1)

        # FFN
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x

    def extra_repr(self) -> str:
        return f"dim={self.dim}, input_resolution={self.input_resolution}, num_heads={self.num_heads}, " \
               f"mlp_ratio={self.mlp_ratio}"







class CAB(nn.Module):

    def __init__(self, num_feat, compress_ratio=3, squeeze_factor=12):
        super(CAB, self).__init__()

        self.cab = nn.Sequential(
            nn.Conv2d(num_feat, num_feat // compress_ratio, 3, 1, 1),
            nn.GELU(),
            nn.Conv2d(num_feat // compress_ratio, num_feat, 3, 1, 1),
            ChannelAttention(num_feat, squeeze_factor)
            )

    def forward(self, x):
        return self.cab(x)

class ChannelAttention(nn.Module):
    """Channel attention used in RCAN.
    Args:
        num_feat (int): Channel number of intermediate features.
        squeeze_factor (int): Channel squeeze factor. Default: 16.
    """

    def __init__(self, num_feat, squeeze_factor=16):
        super(ChannelAttention, self).__init__()
        self.attention = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(num_feat, num_feat // squeeze_factor, 1, padding=0),
            nn.ReLU(inplace=True),
            nn.Conv2d(num_feat // squeeze_factor, num_feat, 1, padding=0),
            nn.Sigmoid())

    def forward(self, x):
        y = self.attention(x)
        return x * y




## Multi-Scale Feed-Forward Network (MSFN)
class MSFN(nn.Module):
    def __init__(self, dim, ffn_expansion_factor, bias):
        super(MSFN, self).__init__()

        hidden_features = int(dim*ffn_expansion_factor)

        self.project_in = nn.Conv3d(dim, hidden_features*3, kernel_size=(1,1,1), bias=bias)

        self.dwconv1 = nn.Conv3d(hidden_features, hidden_features, kernel_size=(3,3,3), stride=1, dilation=1, padding=1, groups=hidden_features, bias=bias)
        # self.dwconv2 = nn.Conv3d(hidden_features, hidden_features, kernel_size=(3,3,3), stride=1, dilation=2, padding=2, groups=hidden_features, bias=bias)
        # self.dwconv3 = nn.Conv3d(hidden_features, hidden_features, kernel_size=(3,3,3), stride=1, dilation=3, padding=3, groups=hidden_features, bias=bias)
        self.dwconv2 = nn.Conv2d(hidden_features, hidden_features, kernel_size=(3,3), stride=1, dilation=2, padding=2, groups=hidden_features, bias=bias)
        self.dwconv3 = nn.Conv2d(hidden_features, hidden_features, kernel_size=(3,3), stride=1, dilation=3, padding=3, groups=hidden_features, bias=bias)


        self.project_out = nn.Conv3d(hidden_features, dim, kernel_size=(1,1,1), bias=bias)

    def forward(self, x):
        x = x.unsqueeze(2)
        x = self.project_in(x)
        x1,x2,x3 = x.chunk(3, dim=1)
        x1 = self.dwconv1(x1).squeeze(2)
        x2 = self.dwconv2(x2.squeeze(2))
        x3 = self.dwconv3(x3.squeeze(2))
        # x1 = self.dwconv1(x1)
        # x2 = self.dwconv2(x2)
        # x3 = self.dwconv3(x3)
        x = F.gelu(x1)*x2*x3
        x = x.unsqueeze(2)
        x = self.project_out(x)
        x = x.squeeze(2)      
        return x



class HybridGate(nn.Module):
    '''
    hybrid feed-forward with channel attention and MLP layer
    '''
    def __init__(self, dim, mlp_ratio):# mlp_ratio = 2.
        super(HybridGate, self).__init__()
        expand_dim = dim * 2
        mlp_hidden_dim = int(dim * mlp_ratio)

        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, out_features=dim)
        self.ca = CAB(num_feat=dim)

        self.expand = nn.Conv2d(in_channels=dim, out_channels=expand_dim, kernel_size=1, padding=0, stride=1, bias=True)
        # self.soomth = nn.Conv2d(in_channels=dim, out_channels=dim, kernel_size=1, padding=0, stride=1, groups=1, bias=True)
   
    def forward(self, x):
        b, c, h, w = x.shape

        x = self.expand(x)
        x1, x2 = x.chunk(2, dim=1)
        x1 = self.ca(x1)

        x2 = x2.view(b, -1, c)
        x2 = self.mlp(x2)
        x2 = x2.view(b, c, h, w)

        out = x1 * x2
        # out = self.soomth(out)
        return out
    
class RoPE(torch.nn.Module):
    r"""Rotary Positional Embedding.
    """
    def __init__(self, shape, base=10000):
        super(RoPE, self).__init__()

        channel_dims, feature_dim = shape[:-1], shape[-1]
        k_max = feature_dim // (2 * len(channel_dims))

        assert feature_dim % k_max == 0

        # angles
        theta_ks = 1 / (base ** (torch.arange(k_max) / k_max))
        angles = torch.cat([t.unsqueeze(-1) * theta_ks for t in torch.meshgrid([torch.arange(d) for d in channel_dims], indexing='ij')], dim=-1)

        # rotation
        rotations_re = torch.cos(angles).unsqueeze(dim=-1)
        rotations_im = torch.sin(angles).unsqueeze(dim=-1)
        rotations = torch.cat([rotations_re, rotations_im], dim=-1)
        self.register_buffer('rotations', rotations)

    def forward(self, x):
        if x.dtype != torch.float32:
            x = x.to(torch.float32)
        x = torch.view_as_complex(x.reshape(*x.shape[:-1], -1, 2))
        pe_x = torch.view_as_complex(self.rotations) * x
        return torch.view_as_real(pe_x).flatten(-2)
def custom_complex_normalization(input_tensor, dim=-1):
    real_part = input_tensor.real
    imag_part = input_tensor.imag
    norm_real = F.softmax(real_part, dim=dim)
    norm_imag = F.softmax(imag_part, dim=dim)

    normalized_tensor = torch.complex(norm_real, norm_imag)

    return normalized_tensor

class Attention_F(nn.Module):
    def __init__(self, dim, num_heads, bias,):
        super(Attention_F, self).__init__()
        self.num_heads = num_heads
        self.temperature = nn.Parameter(torch.ones(num_heads, 1, 1))
        self.project_out = nn.Conv2d(dim*2, dim, kernel_size=1, bias=bias)
        self.weight = nn.Sequential(
            nn.Conv2d(dim, dim // 16, 1, bias=True),
            nn.BatchNorm2d(dim // 16),
            nn.ReLU(True),
            nn.Conv2d(dim // 16, dim, 1, bias=True),
            nn.Sigmoid())
    def forward(self, x):
        b, c, h, w = x.shape

        q_f = torch.fft.fft2(x.float())
        k_f = torch.fft.fft2(x.float())
        v_f = torch.fft.fft2(x.float())
        tepqkv = torch.fft.fft2(x.float())

        q_f = rearrange(q_f, 'b (head c) h w -> b head c (h w)', head=self.num_heads)
        k_f = rearrange(k_f, 'b (head c) h w -> b head c (h w)', head=self.num_heads)
        v_f = rearrange(v_f, 'b (head c) h w -> b head c (h w)', head=self.num_heads)

        q_f = torch.nn.functional.normalize(q_f, dim=-1)
        k_f = torch.nn.functional.normalize(k_f, dim=-1)
        attn_f = (q_f @ k_f.transpose(-2, -1)) * self.temperature
        attn_f = custom_complex_normalization(attn_f, dim=-1)
        out_f = torch.abs(torch.fft.ifft2(attn_f @ v_f))
        out_f = rearrange(out_f, 'b head c (h w) -> b (head c) h w', head=self.num_heads, h=h, w=w)
        out_f_l = torch.abs(torch.fft.ifft2(self.weight(tepqkv.real)*tepqkv))
        out = self.project_out(torch.cat((out_f,out_f_l),1))
        return out

class Attention_S(nn.Module):
    def __init__(self, dim, num_heads, bias,):
        super(Attention_S, self).__init__()
        self.num_heads = num_heads
        self.temperature = nn.Parameter(torch.ones(num_heads, 1, 1))

        self.qkv1conv_1 = nn.Conv2d(dim,dim,kernel_size=1)
        self.qkv2conv_1 = nn.Conv2d(dim, dim, kernel_size=1)
        self.qkv3conv_1 = nn.Conv2d(dim, dim, kernel_size=1)


        self.qkv1conv_3 = nn.Conv2d(dim, dim//2, kernel_size=3, stride=1, padding=1, groups=dim//2, bias=bias)
        self.qkv2conv_3 = nn.Conv2d(dim, dim//2, kernel_size=3, stride=1, padding=1, groups=dim//2, bias=bias)
        self.qkv3conv_3 = nn.Conv2d(dim, dim//2, kernel_size=3, stride=1, padding=1, groups=dim//2, bias=bias)

        self.qkv1conv_5 = nn.Conv2d(dim, dim // 2, kernel_size=5, stride=1, padding=2, groups=dim//2, bias=bias)
        self.qkv2conv_5 = nn.Conv2d(dim, dim // 2, kernel_size=5, stride=1, padding=2, groups=dim//2, bias=bias)
        self.qkv3conv_5 = nn.Conv2d(dim, dim // 2, kernel_size=5, stride=1, padding=2, groups=dim//2, bias=bias)


        self.conv_3      = nn.Conv2d(dim, dim//2, kernel_size=3, stride=1, padding=1, groups=dim//2, bias=bias)
        self.conv_5      = nn.Conv2d(dim, dim // 2, kernel_size=5, stride=1, padding=2, groups=dim//2, bias=bias)
        self.project_out = nn.Conv2d(dim*2, dim, kernel_size=1, bias=bias)

    def forward(self, x):
        b, c, h, w = x.shape
        q_s = torch.cat((self.qkv1conv_3(self.qkv1conv_1(x)),self.qkv1conv_5(self.qkv1conv_1(x))),1)
        k_s = torch.cat((self.qkv2conv_3(self.qkv2conv_1(x)),self.qkv2conv_5(self.qkv2conv_1(x))),1)
        v_s = torch.cat((self.qkv3conv_3(self.qkv3conv_1(x)),self.qkv3conv_5(self.qkv3conv_1(x))),1)

        q_s = rearrange(q_s, 'b (head c) h w -> b head c (h w)', head=self.num_heads)
        k_s = rearrange(k_s, 'b (head c) h w -> b head c (h w)', head=self.num_heads)
        v_s = rearrange(v_s, 'b (head c) h w -> b head c (h w)', head=self.num_heads)

        q_s = torch.nn.functional.normalize(q_s, dim=-1)
        k_s = torch.nn.functional.normalize(k_s, dim=-1)
        attn_s = (q_s @ k_s.transpose(-2, -1)) * self.temperature
        attn_s = attn_s.softmax(dim=-1)
        out_s = (attn_s @ v_s)
        out_s = rearrange(out_s, 'b head c (h w) -> b (head c) h w', head=self.num_heads, h=h, w=w)
        out_s_l = torch.cat((self.conv_3(x),self.conv_5(x)),1)
        out = self.project_out(torch.cat((out_s,out_s_l),1))

        return out

    def initialize(self):
        weight_init(self)

class Module1(nn.Module):
    def __init__(self, mode='dilation', dim=128, num_heads=8, ffn_expansion_factor=4, bias=False,
                 LayerNorm_type='WithBias'):
        super(Module1, self).__init__()
        self.project_out = nn.Conv2d(dim * 2, dim, kernel_size=1, bias=bias)
        self.norm1 = LayerNorm(dim, LayerNorm_type)
        self.attn_S = Attention_S(dim, num_heads, bias)
        self.attn_F = Attention_F(dim, num_heads, bias)
        self.norm2 = LayerNorm(dim, LayerNorm_type)
        self.ffn = FeedForward(dim, ffn_expansion_factor, bias)

    def forward(self, x):
        x = x + torch.add(self.attn_F(self.norm1(x)),self.attn_S(self.norm1(x)))
        x = x + self.ffn(self.norm2(x))
        return x


class ETB(nn.Module): # ETB (Entanglement Transformer Block)
    def __init__(self, in_channel, out_channel):
        super(ETB, self).__init__()
        self.conv1 = nn.Sequential(
            nn.Conv2d(in_channel, out_channel, 1), nn.BatchNorm2d(out_channel),nn.ReLU(True)
        )
        self.reduce  = nn.Sequential(
            nn.Conv2d(out_channel*2, out_channel, 1),nn.BatchNorm2d(out_channel),nn.ReLU(True)
        )
        self.relu = nn.ReLU(True)
        self.Module1 = Module1(dim=out_channel)

    def forward(self, x):
        x0 = self.conv1(x)
        x_FT = self.Module1(x0)
        x    = self.reduce(torch.cat((x0,x_FT),1))+x0
        return x


class LinearAttention(nn.Module):
    r""" Linear Attention with LePE and RoPE.

    Args:
        dim (int): Number of input channels.
        num_heads (int): Number of attention heads.
        qkv_bias (bool, optional):  If True, add a learnable bias to query, key, value. Default: True
    """

    
    def __init__(self, dim, input_resolution, num_heads, qkv_bias=True, **kwargs):

        super().__init__()
        self.dim = dim
        self.input_resolution = input_resolution
        self.num_heads = num_heads
        self.qk = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.elu = nn.ELU()
        self.lepe = nn.Conv2d(dim, dim, 3, padding=1, groups=dim)
        self.rope = RoPE(shape=(input_resolution[0], input_resolution[1], dim))

    def forward(self, x):
        """
        Args:
            x: input features with shape of (B, N, C)
        """
        b, n, c = x.shape
        h = int(n ** 0.5)
        w = int(n ** 0.5)
        num_heads = self.num_heads
        head_dim = c // num_heads

        qk = self.qk(x).reshape(b, n, 2, c).permute(2, 0, 1, 3)
        q, k, v = qk[0], qk[1], x
        # q, k, v: b, n, c

        q = self.elu(q) + 1.0
        k = self.elu(k) + 1.0
        q_rope = self.rope(q.reshape(b, h, w, c)).reshape(b, n, num_heads, head_dim).permute(0, 2, 1, 3)
        k_rope = self.rope(k.reshape(b, h, w, c)).reshape(b, n, num_heads, head_dim).permute(0, 2, 1, 3)
        q = q.reshape(b, n, num_heads, head_dim).permute(0, 2, 1, 3)
        k = k.reshape(b, n, num_heads, head_dim).permute(0, 2, 1, 3)
        v = v.reshape(b, n, num_heads, head_dim).permute(0, 2, 1, 3)

        z = 1 / (q @ k.mean(dim=-2, keepdim=True).transpose(-2, -1) + 1e-6)
        kv = (k_rope.transpose(-2, -1) * (n ** -0.5)) @ (v * (n ** -0.5))
        x = q_rope @ kv * z

        x = x.transpose(1, 2).reshape(b, n, c)
        v = v.transpose(1, 2).reshape(b, h, w, c).permute(0, 3, 1, 2)
        x = x + self.lepe(v).permute(0, 2, 3, 1).reshape(b, n, c)

        return x

    def extra_repr(self) -> str:
        return f'dim={self.dim}, num_heads={self.num_heads}'


class FSFMB(nn.Module):
     def __init__(self, dim,out_channel, input_resolution, num_heads, mlp_ratio=4., qkv_bias=True, drop=0., drop_path=0.,
                 act_layer=nn.GELU, norm_layer=nn.LayerNorm, sr_ratio = 1, **kwargs):
        super().__init__()

        self.dim = dim
        self.input_resolution = input_resolution
        self.num_heads = num_heads
        self.mlp_ratio = mlp_ratio

        self.cpe1 = nn.Conv2d(dim, dim, 3, padding=1, groups=dim)
        self.norm1 = norm_layer(dim)
        self.in_proj = nn.Linear(dim, dim)
        self.act_proj = nn.Linear(dim, dim)
        self.dwc = nn.Conv2d(dim, dim, 3, padding=1, groups=dim)
        self.act = nn.SiLU()
        self.attn_s = LinearAttention_B(dim=dim, input_resolution=input_resolution, num_heads=num_heads, qkv_bias=qkv_bias, sr_ratio=sr_ratio)
        self.out_proj = nn.Linear(dim, dim)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()

        self.cpe2 = nn.Conv2d(dim, dim, 3, padding=1, groups=dim)
        self.norm2 = norm_layer(dim)
        self.mlp = Mlp(in_features=dim, hidden_features=int(dim * mlp_ratio), act_layer=act_layer, drop=drop)

        self.project_out = nn.Conv2d(dim , out_channel, kernel_size=1, bias=False)

        self.drop_path = DropPath(drop_path)

        self.norm = nn.BatchNorm2d(dim)

        self.dwt = DWTForward(J=1, mode='zero', wave='haar')
        self.idwt = DWTInverse(mode='zero', wave='haar')

        self.weight = nn.Sequential(
            nn.Conv2d(dim, dim // 16, 1, bias=True),
            nn.BatchNorm2d(dim // 16),
            nn.ReLU(True),
            nn.Conv2d(dim // 16, dim, 1, bias=True),
            nn.Sigmoid())

        self.softmax = Softmax(dim=-1)

        self.relu = nn.ReLU(True)
        
        self.conv1 = nn.Sequential(
            nn.Conv2d(dim, out_channel, 1), nn.BatchNorm2d(out_channel),nn.ReLU(True)
        )

        self.temperature = nn.Parameter(torch.ones(self.num_heads, 1, 1))
        self.ffn = FeedForward(dim, 4, False)
        self.reduce  = nn.Sequential(
            nn.Conv2d(out_channel*2, out_channel, 1),nn.BatchNorm2d(out_channel),nn.ReLU(True)
        )
        self.dwconv_3  = nn.Sequential(
            nn.Conv2d(dim,dim,kernel_size=1),
            nn.Conv2d(dim, dim//2, kernel_size=3, stride=1, padding=1, groups=dim//2, bias=False)
        )
        
        self.dwconv_5  =  nn.Sequential(
            nn.Conv2d(dim,dim,kernel_size=1),
            nn.Conv2d(dim, dim // 2, kernel_size=5, stride=1, padding=2, groups=dim//2, bias=False)
        )
        
 

     def forward(self, x):
        H, W = self.input_resolution
        B, C, H, W = x.shape
        L = H*W
        x_0 = self.conv1(x)
        

        x = x.flatten(2).permute(0, 2, 1) + self.cpe1(x).flatten(2).permute(0, 2, 1)
        shortcut = x
        tepx = torch.fft.fft2(x.reshape(B, H, W, C).permute(0, 3, 1, 2).float())
        fmt = self.relu(self.norm(torch.abs(torch.fft.ifft2(self.weight(tepx.real) * tepx)))).flatten(2).permute(0, 2, 1)
        

        x_s = self.norm1(x)
        x_s3 = self.dwconv_3(x_s.reshape(B, H, W, C).permute(0, 3, 1, 2)).flatten(2).permute(0, 2, 1)
        x_s5 = self.dwconv_5(x_s.reshape(B, H, W, C).permute(0, 3, 1, 2)).flatten(2).permute(0, 2, 1)
        act_res = self.act(self.act_proj(x_s))
        x_s3 = self.in_proj(x_s3).view(B, H, W, C // 2)
        x_s3 = self.act(self.dwc(x_s3.permute(0, 3, 1, 2))).permute(0, 2, 3, 1).view(B, L, C // 2)
        x_s5 = self.in_proj(x_s5).view(B, H, W, C // 2)
        x_s5 = self.act(self.dwc(x_s5.permute(0, 3, 1, 2))).permute(0, 2, 3, 1).view(B, L, C // 2)


        # Linear Attention
        x_s3 = self.attn(x_s3)
        x_s5 = self.attn(x_s5)
        x_s = torch.cat((x_s3,x_s5),2)

        x_s = self.out_proj(x_s * act_res)
        x = shortcut + self.drop_path(x) + fmt
        x = x + self.cpe2(x.reshape(B, H, W, C).permute(0, 3, 1, 2)).flatten(2).permute(0, 2, 1)

        tepx = torch.fft.fft2(x.reshape(B, H, W, C).permute(0, 3, 1, 2).float())
        fmt = self.relu(self.norm(torch.abs(torch.fft.ifft2(self.weight(tepx.real) * tepx)))).flatten(2).permute(0, 2, 1)

        # FFN
        x = x + self.drop_path(self.ffn(self.norm2(x).reshape(B, H, W, C).permute(0, 3, 1, 2))).flatten(2).permute(0, 2, 1) + fmt
        x = x.reshape(B, H, W, C).permute(0, 3, 1, 2) # B C H W
        x = self.project_out(x)

        x    = self.reduce(torch.cat((x_0,x),1))+x_0

        return x

     def extra_repr(self) -> str:
        return f"dim={self.dim}, input_resolution={self.input_resolution}, num_heads={self.num_heads}, " \
               f"mlp_ratio={self.mlp_ratio}"












class PFAFM(nn.Module): # Pyramid Frequency Attention Fusion Module
    def __init__(self, dim,in_dim):
        super(PFAFM, self).__init__()
        self.down_conv = nn.Sequential(nn.Conv2d(dim,in_dim , 3,padding=1),nn.BatchNorm2d(in_dim),
             nn.ReLU(True))
        down_dim = in_dim // 2

        self.conv1 = nn.Sequential(
            nn.Conv2d(in_dim, down_dim, kernel_size=1), nn.BatchNorm2d(down_dim), nn.ReLU(True)
        )


        self.conv2 = nn.Sequential(
            nn.Conv2d(down_dim, down_dim, kernel_size=3, dilation=3, padding=3), nn.BatchNorm2d(down_dim), nn.ReLU(True)
        )
        self.query_conv2 = nn.Conv2d(in_channels=down_dim, out_channels=down_dim//8, kernel_size=1)
        self.key_conv2 = nn.Conv2d(in_channels=down_dim, out_channels=down_dim//8, kernel_size=1)
        self.value_conv2 = nn.Conv2d(in_channels=down_dim, out_channels=down_dim, kernel_size=1)
        self.gamma2 = nn.Parameter(torch.zeros(1))


        self.conv3 = nn.Sequential(
            nn.Conv2d(down_dim, down_dim, kernel_size=3, dilation=5, padding=5), nn.BatchNorm2d(down_dim), nn.ReLU(True)
        )
        self.query_conv3 = nn.Conv2d(in_channels=down_dim, out_channels=down_dim//8, kernel_size=1)
        self.key_conv3 = nn.Conv2d(in_channels=down_dim, out_channels=down_dim//8, kernel_size=1)
        self.value_conv3 =nn.Conv2d(in_channels=down_dim, out_channels=down_dim, kernel_size=1)
        self.gamma3 = nn.Parameter(torch.zeros(1))


        self.conv4 = nn.Sequential(
            nn.Conv2d(down_dim, down_dim, kernel_size=3, dilation=7, padding=7), nn.BatchNorm2d(down_dim), nn.ReLU(True)
        )
        self.query_conv4 = nn.Conv2d(in_channels=down_dim, out_channels=down_dim//8, kernel_size=1)
        self.key_conv4 = nn.Conv2d(in_channels=down_dim, out_channels=down_dim//8, kernel_size=1)
        self.value_conv4 = nn.Conv2d(in_channels=down_dim, out_channels=down_dim, kernel_size=1)
        self.gamma4 = nn.Parameter(torch.zeros(1))

        self.conv5 = nn.Sequential(
            nn.Conv2d(down_dim, down_dim, kernel_size=3, dilation=9, padding=9), nn.BatchNorm2d(down_dim), nn.ReLU(True)
        )
        self.query_conv5 = nn.Conv2d(in_channels=down_dim, out_channels=down_dim//8, kernel_size=1)
        self.key_conv5 = nn.Conv2d(in_channels=down_dim, out_channels=down_dim//8, kernel_size=1)
        self.value_conv5 = nn.Conv2d(in_channels=down_dim, out_channels=down_dim, kernel_size=1)
        self.gamma5 = nn.Parameter(torch.zeros(1))


        self.conv6 = nn.Sequential(
            nn.Conv2d(in_dim, down_dim, kernel_size=1),nn.BatchNorm2d(down_dim),  nn.ReLU(True)  #如果batch=1 ，进行batchnorm会有问题
        )

        self.fuse = nn.Sequential(
            nn.Conv2d(6 * down_dim, down_dim, kernel_size=1), nn.BatchNorm2d(down_dim), nn.ReLU(True)
        )

        self.out = nn.Sequential(
            nn.Conv2d(down_dim, down_dim//2, kernel_size=3, padding=1), nn.BatchNorm2d(down_dim//2), nn.ReLU(True),
            nn.Conv2d(down_dim//2, 1, kernel_size=1)
        )


        self.temperature = nn.Parameter(torch.ones(8, 1, 1))
        self.project_out = nn.Conv2d(down_dim*2, down_dim, kernel_size=1, bias=False)

        self.weight = nn.Sequential(
            nn.Conv2d(down_dim, down_dim // 16, 1, bias=True),
            nn.BatchNorm2d(down_dim // 16),
            nn.ReLU(True),
            nn.Conv2d(down_dim // 16, down_dim, 1, bias=True),
            nn.Sigmoid())

        self.softmax = Softmax(dim=-1)
        self.norm = nn.BatchNorm2d(down_dim)
        self.relu = nn.ReLU(True)
        self.num_heads = 16

    def forward(self, x):
        x = self.down_conv(x)
        conv1 = self.conv1(x)

       
        conv2 = self.conv2(conv1)
        b, c, h, w = conv2.shape

        q_f_2 = torch.fft.fft2(conv2.float())
        k_f_2 = torch.fft.fft2(conv2.float())
        v_f_2 = torch.fft.fft2(conv2.float())
        tepqkv = torch.fft.fft2(conv2.float())

        q_f_2 = rearrange(q_f_2, 'b (head c) h w -> b head c (h w)', head=self.num_heads)
        k_f_2 = rearrange(k_f_2, 'b (head c) h w -> b head c (h w)', head=self.num_heads)
        v_f_2 = rearrange(v_f_2, 'b (head c) h w -> b head c (h w)', head=self.num_heads)

        q_f_2 = torch.nn.functional.normalize(q_f_2, dim=-1)
        k_f_2 = torch.nn.functional.normalize(k_f_2, dim=-1)
        attn_f_2 = (q_f_2 @ k_f_2.transpose(-2, -1)) * self.temperature
        attn_f_2 = custom_complex_normalization(attn_f_2, dim=-1)
        out_f_2 = torch.abs(torch.fft.ifft2(attn_f_2 @ v_f_2))
        out_f_2 = rearrange(out_f_2, 'b head c (h w) -> b (head c) h w', head=self.num_heads, h=h, w=w)
        out_f_l_2 = torch.abs(torch.fft.ifft2(self.weight(tepqkv.real)*tepqkv))
        out_2 = self.project_out(torch.cat((out_f_2,out_f_l_2),1))
        F_2 = torch.add(out_2, conv2)



        conv3 = self.conv3(conv1+F_2)
        b, c, h, w = conv3.shape

        q_f_3 = torch.fft.fft2(conv3.float())
        k_f_3 = torch.fft.fft2(conv3.float())
        v_f_3 = torch.fft.fft2(conv3.float())
        tepqkv = torch.fft.fft2(conv3.float())

        q_f_3 = rearrange(q_f_3, 'b (head c) h w -> b head c (h w)', head=self.num_heads)
        k_f_3 = rearrange(k_f_3, 'b (head c) h w -> b head c (h w)', head=self.num_heads)
        v_f_3 = rearrange(v_f_3, 'b (head c) h w -> b head c (h w)', head=self.num_heads)

        q_f_3 = torch.nn.functional.normalize(q_f_3, dim=-1)
        k_f_3 = torch.nn.functional.normalize(k_f_3, dim=-1)
        attn_f_3 = (q_f_3 @ k_f_3.transpose(-2, -1)) * self.temperature
        attn_f_3 = custom_complex_normalization(attn_f_3, dim=-1)
        out_f_3 = torch.abs(torch.fft.ifft2(attn_f_3 @ v_f_3))
        out_f_3 = rearrange(out_f_3, 'b head c (h w) -> b (head c) h w', head=self.num_heads, h=h, w=w)
        out_f_l_3 = torch.abs(torch.fft.ifft2(self.weight(tepqkv.real)*tepqkv))
        out_3 = self.project_out(torch.cat((out_f_3,out_f_l_3),1))
        F_3 = torch.add(out_3, conv3)



        conv4 = self.conv4(conv1+F_3)
        b, c, h, w = conv4.shape

        q_f_4 = torch.fft.fft2(conv4.float())
        k_f_4 = torch.fft.fft2(conv4.float())
        v_f_4 = torch.fft.fft2(conv4.float())
        tepqkv = torch.fft.fft2(conv4.float())

        q_f_4 = rearrange(q_f_4, 'b (head c) h w -> b head c (h w)', head=self.num_heads)
        k_f_4 = rearrange(k_f_4, 'b (head c) h w -> b head c (h w)', head=self.num_heads)
        v_f_4 = rearrange(v_f_4, 'b (head c) h w -> b head c (h w)', head=self.num_heads)

        q_f_4 = torch.nn.functional.normalize(q_f_4, dim=-1)
        k_f_4 = torch.nn.functional.normalize(k_f_4, dim=-1)
        attn_f_4 = (q_f_4 @ k_f_4.transpose(-2, -1)) * self.temperature
        attn_f_4 = custom_complex_normalization(attn_f_4, dim=-1)
        out_f_4 = torch.abs(torch.fft.ifft2(attn_f_4 @ v_f_4))
        out_f_4 = rearrange(out_f_4, 'b head c (h w) -> b (head c) h w', head=self.num_heads, h=h, w=w)
        out_f_l_4 = torch.abs(torch.fft.ifft2(self.weight(tepqkv.real)*tepqkv))
        out_4 = self.project_out(torch.cat((out_f_4,out_f_l_4),1))
        F_4 = torch.add(out_4, conv4)

        conv5 = self.conv5(conv1+F_4)
        b, c, h, w = conv5.shape

        q_f_5 = torch.fft.fft2(conv5.float())
        k_f_5 = torch.fft.fft2(conv5.float())
        v_f_5 = torch.fft.fft2(conv5.float())
        tepqkv = torch.fft.fft2(conv5.float())

        q_f_5 = rearrange(q_f_5, 'b (head c) h w -> b head c (h w)', head=self.num_heads)
        k_f_5 = rearrange(k_f_5, 'b (head c) h w -> b head c (h w)', head=self.num_heads)
        v_f_5 = rearrange(v_f_5, 'b (head c) h w -> b head c (h w)', head=self.num_heads)

        q_f_5 = torch.nn.functional.normalize(q_f_5, dim=-1)
        k_f_5 = torch.nn.functional.normalize(k_f_5, dim=-1)
        attn_f_5 = (q_f_5 @ k_f_5.transpose(-2, -1)) * self.temperature
        attn_f_5 = custom_complex_normalization(attn_f_5, dim=-1)
        out_f_5 = torch.abs(torch.fft.ifft2(attn_f_5 @ v_f_5))
        out_f_5 = rearrange(out_f_5, 'b head c (h w) -> b (head c) h w', head=self.num_heads, h=h, w=w)
        out_f_l_5 = torch.abs(torch.fft.ifft2(self.weight(tepqkv.real)*tepqkv))
        out_5 = self.project_out(torch.cat((out_f_5,out_f_l_5),1))
        F_5 = torch.add(out_5, conv5)



        conv5 = F.upsample(self.conv6(F.adaptive_avg_pool2d(x, 1)), size=x.size()[2:], mode='bilinear') # 如果batch设为1，这里就会有问题。

        F_out = self.out(self.fuse(torch.cat((conv1, F_2, F_3,F_4,F_5, conv5), 1)))

        return F_out





class JDPM(nn.Module): # JDPM (Joint Domain Perception Module)
    def __init__(self, channels, in_channels):
        super(JDPM, self).__init__()

        self.conv1 = nn.Sequential(
            nn.Conv2d(channels, in_channels, 1), nn.BatchNorm2d(in_channels), nn.ReLU(True)
        )

        self.Dconv3 = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, 1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, 3, padding=3,dilation=3), nn.BatchNorm2d(in_channels), nn.ReLU(True)
        )

        self.Dconv5 = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, 1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, 3, padding=5,dilation=5), nn.BatchNorm2d(in_channels), nn.ReLU(True)
        )
        self.Dconv7 = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, 1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, 3, padding=7,dilation=7), nn.BatchNorm2d(in_channels), nn.ReLU(True)
        )
        self.Dconv9 = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, 1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, 3, padding=9,dilation=9), nn.BatchNorm2d(in_channels),nn.ReLU(True)
        )

        self.reduce = nn.Sequential(
            nn.Conv2d(in_channels * 5, in_channels, 1), nn.BatchNorm2d(in_channels),nn.ReLU(True)
        )

        self.out = nn.Sequential(
            nn.Conv2d(in_channels, in_channels//2, kernel_size=3, padding=1), nn.BatchNorm2d(in_channels//2), nn.ReLU(True),
            nn.Conv2d(in_channels//2, 1, kernel_size=1)
        )

        self.weight = nn.Sequential(
            nn.Conv2d(in_channels, in_channels // 16, 1, bias=True),
            nn.BatchNorm2d(in_channels // 16),
            nn.ReLU(True),
            nn.Conv2d(in_channels // 16, in_channels, 1, bias=True),
            nn.Sigmoid())

        self.norm = nn.BatchNorm2d(in_channels)
        self.relu = nn.ReLU(True)

    def forward(self, F1):

       F1_input  = self.conv1(F1)

       F1_3_s = self.Dconv3(F1_input)
       F1_3_f = self.relu(self.norm(torch.abs(torch.fft.ifft2(self.weight(torch.fft.fft2(F1_3_s.float()).real)*torch.fft.fft2(F1_3_s.float())))))
       F1_3 = torch.add(F1_3_s,F1_3_f)

       F1_5_s = self.Dconv5(F1_input + F1_3)
       F1_5_f = self.relu(self.norm(torch.abs(torch.fft.ifft2(self.weight(torch.fft.fft2(F1_5_s.float()).real)*torch.fft.fft2(F1_5_s.float())))))
       F1_5 = torch.add(F1_5_s, F1_5_f)

       F1_7_s = self.Dconv7(F1_input + F1_5)
       F1_7_f = self.relu(self.norm(torch.abs(torch.fft.ifft2(self.weight(torch.fft.fft2(F1_7_s.float()).real)*torch.fft.fft2(F1_7_s.float())))))
       F1_7 = torch.add(F1_7_s, F1_7_f)

       F1_9_s = self.Dconv9(F1_input + F1_7)
       F1_9_f = self.relu(self.norm(torch.abs(torch.fft.ifft2(self.weight(torch.fft.fft2(F1_9_s.float()).real)*torch.fft.fft2(F1_9_s.float())))))
       F1_9 = torch.add(F1_9_s, F1_9_f)

       F_out = self.out(self.reduce(torch.cat((F1_3,F1_5,F1_7,F1_9,F1_input),1)) + F1_input )

       return F_out




class DRD_1(nn.Module): # DRP (Dual-domain Reverse Parser)
    def __init__(self, in_channels, mid_channels):
        super(DRD_1, self).__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(in_channels * 2, in_channels, kernel_size=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels), nn.ReLU(True)
        )

        self.out = nn.Sequential(
            nn.Conv2d(in_channels * 2, mid_channels, kernel_size=3, padding=1), nn.BatchNorm2d(mid_channels),nn.ReLU(True),
            nn.Conv2d(mid_channels, 1, kernel_size=1)
        )

        self.conv3 = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, kernel_size=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),nn.ReLU(True),
        )

        self.weight = nn.Sequential(
            nn.Conv2d(in_channels, in_channels // 16, 1, bias=True),
            nn.BatchNorm2d(in_channels // 16),
            nn.ReLU(True),
            nn.Conv2d(in_channels // 16, in_channels, 1, bias=True),
            nn.Sigmoid())

        self.norm = nn.BatchNorm2d(in_channels)
        self.relu = nn.ReLU(in_channels)

    def forward(self, X, prior_cam):
        prior_cam = F.interpolate(prior_cam, size=X.size()[2:], mode='bilinear',align_corners=True)

        FI  = X

        yt = self.conv(torch.cat([FI, prior_cam.expand(-1, X.size()[1], -1, -1)], dim=1))

        yt_s = self.conv3(yt)
        yt_out = yt_s

        r_prior_cam_f = torch.abs(torch.fft.fft2(prior_cam))
        r_prior_cam_f = -1 * (torch.sigmoid(r_prior_cam_f)) + 1
        r_prior_cam_s = -1 * (torch.sigmoid(prior_cam)) + 1
        r_prior_cam = r_prior_cam_s + r_prior_cam_f

        y_ra = r_prior_cam.expand(-1, X.size()[1], -1, -1).mul(FI)

        out = torch.cat([y_ra, yt_out], dim=1)  # 2,128,48,48

        y = self.out(out)
        y = y + prior_cam
        return y

class DRD_2(nn.Module): # DRP (Dual-domain Reverse Parser)
    def __init__(self, in_channels, mid_channels):
        super(DRD_2, self).__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(in_channels * 3, in_channels, kernel_size=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),nn.ReLU(True),
        )

        self.conv3 = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, kernel_size=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),nn.ReLU(True),
        )

        self.out = nn.Sequential(
            nn.Conv2d(in_channels * 2, mid_channels, kernel_size=3, padding=1), nn.BatchNorm2d(mid_channels),nn.ReLU(True),
            nn.Conv2d(mid_channels, 1, kernel_size=1)
        )

        self.weight = nn.Sequential(
            nn.Conv2d(in_channels, in_channels // 16, 1, bias=True),
            nn.BatchNorm2d(in_channels // 16),
            nn.ReLU(True),
            nn.Conv2d(in_channels // 16, in_channels, 1, bias=True),
            nn.Sigmoid())

        self.norm = nn.BatchNorm2d(in_channels)
        self.relu = nn.ReLU(True)

    def forward(self, X, x1, prior_cam):
        prior_cam = F.interpolate(prior_cam, size=X.size()[2:], mode='bilinear',align_corners=True)
        x1_prior_cam = F.interpolate(x1, size=X.size()[2:], mode='bilinear', align_corners=True)
        FI = X

        yt = self.conv(torch.cat([FI, prior_cam.expand(-1, X.size()[1], -1, -1), x1_prior_cam.expand(-1, X.size()[1], -1, -1)],dim=1))

        yt_s = self.conv3(yt)
        yt_out = yt_s

        r_prior_cam_f = torch.abs(torch.fft.fft2(prior_cam))
        r_prior_cam_f = -1 * (torch.sigmoid(r_prior_cam_f)) + 1
        r_prior_cam_s = -1 * (torch.sigmoid(prior_cam)) + 1
        r_prior_cam = r_prior_cam_s + r_prior_cam_f

        r1_prior_cam_f = torch.abs(torch.fft.fft2(x1_prior_cam))
        r1_prior_cam_f = -1 * (torch.sigmoid(r1_prior_cam_f)) + 1
        r1_prior_cam_s = -1 * (torch.sigmoid(x1_prior_cam)) + 1
        r1_prior_cam = r1_prior_cam_s + r1_prior_cam_f

        r_prior_cam = r_prior_cam + r1_prior_cam

        y_ra = r_prior_cam.expand(-1, X.size()[1], -1, -1).mul(FI)

        out = torch.cat([y_ra, yt_out], dim=1)

        y = self.out(out)
        y = y + prior_cam + x1_prior_cam
        return y

class DRD_3(nn.Module): # DRP (Dual-domain Reverse Parser)
    def __init__(self, in_channels, mid_channels):
        super(DRD_3, self).__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(in_channels * 4, in_channels, kernel_size=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),nn.ReLU(True),
        )

        self.conv3 = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, kernel_size=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),nn.ReLU(True),
        )

        self.out = nn.Sequential(
            nn.Conv2d(in_channels * 2, mid_channels, kernel_size=3, padding=1), nn.BatchNorm2d(mid_channels),nn.ReLU(True),
            nn.Conv2d(mid_channels, 1, kernel_size=1)
        )

        self.weight = nn.Sequential(
            nn.Conv2d(in_channels, in_channels // 16, 1, bias=True),
            nn.BatchNorm2d(in_channels // 16),
            nn.ReLU(True),
            nn.Conv2d(in_channels // 16, in_channels, 1, bias=True),
            nn.Sigmoid())

        self.norm = nn.BatchNorm2d(in_channels)
        self.relu = nn.ReLU(True)

    def forward(self, X, x1,x2, prior_cam):
        prior_cam = F.interpolate(prior_cam, size=X.size()[2:], mode='bilinear',align_corners=True)  #
        x1_prior_cam = F.interpolate(x1, size=X.size()[2:], mode='bilinear', align_corners=True)
        x2_prior_cam = F.interpolate(x2, size=X.size()[2:], mode='bilinear', align_corners=True)
        FI = X

        yt = self.conv(torch.cat([FI, prior_cam.expand(-1, X.size()[1], -1, -1), x1_prior_cam.expand(-1, X.size()[1], -1, -1),x2_prior_cam.expand(-1, X.size()[1], -1, -1)],dim=1))

        yt_s = self.conv3(yt)
        yt_out = yt_s

        r_prior_cam_f = torch.abs(torch.fft.fft2(prior_cam))
        r_prior_cam_f = -1 * (torch.sigmoid(r_prior_cam_f)) + 1
        r_prior_cam_s = -1 * (torch.sigmoid(prior_cam)) + 1
        r_prior_cam = r_prior_cam_s + r_prior_cam_f

        r1_prior_cam_f = torch.abs(torch.fft.fft2(x1_prior_cam))
        r1_prior_cam_f = -1 * (torch.sigmoid(r1_prior_cam_f)) + 1
        r1_prior_cam_s = -1 * (torch.sigmoid(x1_prior_cam)) + 1
        r1_prior_cam1 = r1_prior_cam_s + r1_prior_cam_f

        r2_prior_cam_f = torch.abs(torch.fft.fft2(x2_prior_cam))
        r2_prior_cam_f = -1 * (torch.sigmoid(r2_prior_cam_f)) + 1
        r2_prior_cam_s = -1 * (torch.sigmoid(x2_prior_cam)) + 1
        r1_prior_cam2 = r2_prior_cam_s + r2_prior_cam_f

        r_prior_cam = r_prior_cam + r1_prior_cam1 + r1_prior_cam2

        y_ra = r_prior_cam.expand(-1, X.size()[1], -1, -1).mul(FI)

        out = torch.cat([y_ra, yt_out], dim=1)

        y = self.out(out)

        y = y + prior_cam + x1_prior_cam + x2_prior_cam

        return y



class DRP_1(nn.Module): # DRP (Dual-domain Reverse Parser)
    def __init__(self, in_channels, mid_channels):
        super(DRP_1, self).__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(in_channels * 2, in_channels, kernel_size=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels), nn.ReLU(True)
        )

        self.out = nn.Sequential(
            nn.Conv2d(in_channels * 2, mid_channels, kernel_size=3, padding=1), nn.BatchNorm2d(mid_channels),nn.ReLU(True),
            nn.Conv2d(mid_channels, 1, kernel_size=1)
        )

        self.conv3 = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, kernel_size=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),nn.ReLU(True),
        )

        self.weight = nn.Sequential(
            nn.Conv2d(in_channels, in_channels // 16, 1, bias=True),
            nn.BatchNorm2d(in_channels // 16),
            nn.ReLU(True),
            nn.Conv2d(in_channels // 16, in_channels, 1, bias=True),
            nn.Sigmoid())

        self.norm = nn.BatchNorm2d(in_channels)
        self.relu = nn.ReLU(in_channels)

    def forward(self, X, prior_cam):
        prior_cam = F.interpolate(prior_cam, size=X.size()[2:], mode='bilinear',align_corners=True)

        FI  = X

        yt = self.conv(torch.cat([FI, prior_cam.expand(-1, X.size()[1], -1, -1)], dim=1))

        yt_s = self.conv3(yt)
        yt_f = self.relu(self.norm(torch.abs(torch.fft.ifft2(self.weight(torch.fft.fft2(yt.float()).real)*torch.fft.fft2(yt.float())))))
        yt_out = torch.add(yt_s,yt_f)

        r_prior_cam_f = torch.abs(torch.fft.fft2(prior_cam))
        r_prior_cam_f = -1 * (torch.sigmoid(r_prior_cam_f)) + 1
        r_prior_cam_s = -1 * (torch.sigmoid(prior_cam)) + 1
        r_prior_cam = r_prior_cam_s + r_prior_cam_f

        y_ra = r_prior_cam.expand(-1, X.size()[1], -1, -1).mul(FI)

        out = torch.cat([y_ra, yt_out], dim=1)  # 2,128,48,48

        y = self.out(out)
        y = y + prior_cam
        return y

class DRP_2(nn.Module): # DRP (Dual-domain Reverse Parser)
    def __init__(self, in_channels, mid_channels):
        super(DRP_2, self).__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(in_channels * 3, in_channels, kernel_size=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),nn.ReLU(True),
        )

        self.conv3 = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, kernel_size=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),nn.ReLU(True),
        )

        self.out = nn.Sequential(
            nn.Conv2d(in_channels * 2, mid_channels, kernel_size=3, padding=1), nn.BatchNorm2d(mid_channels),nn.ReLU(True),
            nn.Conv2d(mid_channels, 1, kernel_size=1)
        )

        self.weight = nn.Sequential(
            nn.Conv2d(in_channels, in_channels // 16, 1, bias=True),
            nn.BatchNorm2d(in_channels // 16),
            nn.ReLU(True),
            nn.Conv2d(in_channels // 16, in_channels, 1, bias=True),
            nn.Sigmoid())

        self.norm = nn.BatchNorm2d(in_channels)
        self.relu = nn.ReLU(True)

    def forward(self, X, x1, prior_cam):
        prior_cam = F.interpolate(prior_cam, size=X.size()[2:], mode='bilinear',align_corners=True)
        x1_prior_cam = F.interpolate(x1, size=X.size()[2:], mode='bilinear', align_corners=True)
        FI = X

        yt = self.conv(torch.cat([FI, prior_cam.expand(-1, X.size()[1], -1, -1), x1_prior_cam.expand(-1, X.size()[1], -1, -1)],dim=1))

        yt_s = self.conv3(yt)
        yt_f = self.relu(self.norm(torch.abs(torch.fft.ifft2(self.weight(torch.fft.fft2(yt.float()).real) * torch.fft.fft2(yt.float())))))
        yt_out = torch.add(yt_s, yt_f)

        r_prior_cam_f = torch.abs(torch.fft.fft2(prior_cam))
        r_prior_cam_f = -1 * (torch.sigmoid(r_prior_cam_f)) + 1
        r_prior_cam_s = -1 * (torch.sigmoid(prior_cam)) + 1
        r_prior_cam = r_prior_cam_s + r_prior_cam_f

        r1_prior_cam_f = torch.abs(torch.fft.fft2(x1_prior_cam))
        r1_prior_cam_f = -1 * (torch.sigmoid(r1_prior_cam_f)) + 1
        r1_prior_cam_s = -1 * (torch.sigmoid(x1_prior_cam)) + 1
        r1_prior_cam = r1_prior_cam_s + r1_prior_cam_f

        r_prior_cam = r_prior_cam + r1_prior_cam

        y_ra = r_prior_cam.expand(-1, X.size()[1], -1, -1).mul(FI)

        out = torch.cat([y_ra, yt_out], dim=1)

        y = self.out(out)
        y = y + prior_cam + x1_prior_cam
        return y

class DRP_3(nn.Module): # DRP (Dual-domain Reverse Parser)
    def __init__(self, in_channels, mid_channels):
        super(DRP_3, self).__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(in_channels * 4, in_channels, kernel_size=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),nn.ReLU(True),
        )

        self.conv3 = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, kernel_size=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, padding=1, stride=1), nn.BatchNorm2d(in_channels),nn.ReLU(True),
        )

        self.out = nn.Sequential(
            nn.Conv2d(in_channels * 2, mid_channels, kernel_size=3, padding=1), nn.BatchNorm2d(mid_channels),nn.ReLU(True),
            nn.Conv2d(mid_channels, 1, kernel_size=1)
        )

        self.weight = nn.Sequential(
            nn.Conv2d(in_channels, in_channels // 16, 1, bias=True),
            nn.BatchNorm2d(in_channels // 16),
            nn.ReLU(True),
            nn.Conv2d(in_channels // 16, in_channels, 1, bias=True),
            nn.Sigmoid())

        self.norm = nn.BatchNorm2d(in_channels)
        self.relu = nn.ReLU(True)

    def forward(self, X, x1,x2, prior_cam):
        prior_cam = F.interpolate(prior_cam, size=X.size()[2:], mode='bilinear',align_corners=True)  #
        x1_prior_cam = F.interpolate(x1, size=X.size()[2:], mode='bilinear', align_corners=True)
        x2_prior_cam = F.interpolate(x2, size=X.size()[2:], mode='bilinear', align_corners=True)
        FI = X

        yt = self.conv(torch.cat([FI, prior_cam.expand(-1, X.size()[1], -1, -1), x1_prior_cam.expand(-1, X.size()[1], -1, -1),x2_prior_cam.expand(-1, X.size()[1], -1, -1)],dim=1))

        yt_s = self.conv3(yt)
        yt_f = self.relu(self.norm(torch.abs(torch.fft.ifft2(self.weight(torch.fft.fft2(yt.float()).real) * torch.fft.fft2(yt.float())))))
        yt_out = torch.add(yt_s, yt_f)

        r_prior_cam_f = torch.abs(torch.fft.fft2(prior_cam))
        r_prior_cam_f = -1 * (torch.sigmoid(r_prior_cam_f)) + 1
        r_prior_cam_s = -1 * (torch.sigmoid(prior_cam)) + 1
        r_prior_cam = r_prior_cam_s + r_prior_cam_f

        r1_prior_cam_f = torch.abs(torch.fft.fft2(x1_prior_cam))
        r1_prior_cam_f = -1 * (torch.sigmoid(r1_prior_cam_f)) + 1
        r1_prior_cam_s = -1 * (torch.sigmoid(x1_prior_cam)) + 1
        r1_prior_cam1 = r1_prior_cam_s + r1_prior_cam_f

        r2_prior_cam_f = torch.abs(torch.fft.fft2(x2_prior_cam))
        r2_prior_cam_f = -1 * (torch.sigmoid(r2_prior_cam_f)) + 1
        r2_prior_cam_s = -1 * (torch.sigmoid(x2_prior_cam)) + 1
        r1_prior_cam2 = r2_prior_cam_s + r2_prior_cam_f

        r_prior_cam = r_prior_cam + r1_prior_cam1 + r1_prior_cam2

        y_ra = r_prior_cam.expand(-1, X.size()[1], -1, -1).mul(FI)

        out = torch.cat([y_ra, yt_out], dim=1)

        y = self.out(out)

        y = y + prior_cam + x1_prior_cam + x2_prior_cam

        return y























