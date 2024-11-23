import timm
from lib.pvt_v2 import pvt_v2_b4
import torch.nn as nn
import torch
import torch.nn.functional as F
from lib_initial.module_FSEL import Module3_1, Module3_2, Module3_3, Module2,Module1_res


'''
backbone: PVT
'''


class Network(nn.Module):
    # PVT based encoder decoder
    def __init__(self, channels=128):
        super(Network, self).__init__()


        self.shared_encoder = pvt_v2_b4()

        pretrained_dict = torch.load('/COD/pre_train_pth/pvt_v2_b4.pth')
        pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in self.shared_encoder.state_dict()}
        self.shared_encoder.load_state_dict(pretrained_dict)

        self.dePixelShuffle = torch.nn.PixelShuffle(2)

        self.up = nn.Sequential(
            nn.Conv2d(channels//4, channels, kernel_size=1),nn.BatchNorm2d(channels),
            nn.Conv2d(channels, channels, kernel_size=3, padding=1),nn.BatchNorm2d(channels),nn.ReLU(True)
        )

        self.Module1_5 = Module1_res(512+channels, channels)
        self.Module1_4 = Module1_res(320+channels, channels)
        self.Module1_3 = Module1_res(128+channels, channels)
        self.Module1_2 = Module1_res(64+channels, channels)

        self.Module2 = Module2(512, channels)

        self.Module3_1 = Module3_1(channels, channels)
        self.Module3_2 = Module3_2(channels, channels)
        self.Module3_3 = Module3_3(channels,channels)

    def forward(self, x):
        image = x

        en_feats = self.shared_encoder(x)
        x4, x3, x2, x1 = en_feats


        p1 = self.Module2(x4)
        x5_4 = p1
        x5_4_1 = x5_4.expand(-1, 128, -1, -1)

        x4   = self.Module1_5(torch.cat((x4,x5_4_1),1))
        x4_up = self.up(self.dePixelShuffle(x4))

        x3   = self.Module1_4(torch.cat((x3,x4_up),1))
        x3_up = self.up(self.dePixelShuffle(x3))

        x2   = self.Module1_3(torch.cat((x2,x3_up),1))
        x2_up = self.up(self.dePixelShuffle(x2))


        x1   = self.Module1_2(torch.cat((x1,x2_up),1))


        x4 = self.Module3_1(x4,x5_4)
        x3 = self.Module3_1(x3,x4)
        x2 = self.Module3_2(x2,x3,x4)
        x1 = self.Module3_3(x1,x2,x3,x4)


        p0 = F.interpolate(p1, size=image.size()[2:], mode='bilinear', align_corners=True)
        f4 = F.interpolate(x4, size=image.size()[2:], mode='bilinear', align_corners=True)
        f3 = F.interpolate(x3, size=image.size()[2:], mode='bilinear', align_corners=True)
        f2 = F.interpolate(x2, size=image.size()[2:], mode='bilinear', align_corners=True)
        f1 = F.interpolate(x1, size=image.size()[2:], mode='bilinear', align_corners=True)


        return p0, f4, f3, f2, f1
