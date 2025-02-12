# Copyright 2021 Xilinx Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import torch
from torch.nn import init
from nn_layers.espnet_utils import *
from nn_layers.efficient_pyramid_pool import EfficientPyrPool
from nn_layers.efficient_pt import EfficientPWConv
from model.classification.espnetv2 import EESPNet
from torch.nn import functional as F


def Deconv_BN_ACT(in_plane, out_plane):
    conv_trans = nn.ConvTranspose2d(in_plane, out_plane ,kernel_size=3, stride=2,padding=1, output_padding=1, bias=False)
    norm =  nn.BatchNorm2d(out_plane)
    act = nn.PReLU(out_plane)
    return nn.Sequential(conv_trans, norm, act)

class ESPNetv2Segmentation(nn.Module):
    '''
    This class defines the ESPNetv2 architecture for the Semantic Segmenation
    '''

    def __init__(self, args, classes=21, dataset='pascal'):
        super().__init__()

        # =============================================================
        #                       BASE NETWORK
        # =============================================================

        #   same as Line27-31 in ESPNetv2, except the dec_planes
        self.base_net = EESPNet(args) #imagenet model
        del self.base_net.classifier
        del self.base_net.level5
        del self.base_net.level5_0
        config = self.base_net.config

        #=============================================================
        #                   SEGMENTATION BASE NETWORK
        #=============================================================
        #
        #  same as Line36-66 in ESPNetv2      
        #   
        dec_feat_dict={
            'pascal': 16,
            'city': 16,
            'coco': 32
        }
        base_dec_planes = dec_feat_dict[dataset]
        dec_planes = [4*base_dec_planes, 3*base_dec_planes, 2*base_dec_planes, classes]
        pyr_plane_proj = min(classes //2, base_dec_planes)

        self.bu_dec_l1 = EfficientPyrPool(in_planes=config[3], proj_planes=pyr_plane_proj,
                                          out_planes=dec_planes[0])
        self.bu_dec_l2 = EfficientPyrPool(in_planes=dec_planes[0], proj_planes=pyr_plane_proj,
                                          out_planes=dec_planes[1])
        self.bu_dec_l3 = EfficientPyrPool(in_planes=dec_planes[1], proj_planes=pyr_plane_proj,
                                          out_planes=dec_planes[2])
        self.bu_dec_l4 = EfficientPyrPool(in_planes=dec_planes[2], proj_planes=pyr_plane_proj,
                                          out_planes=dec_planes[3], last_layer_br=False)

        self.merge_enc_dec_l2 = EfficientPWConv(config[2], dec_planes[0])
        self.merge_enc_dec_l3 = EfficientPWConv(config[1], dec_planes[1])
        self.merge_enc_dec_l4 = EfficientPWConv(config[0], dec_planes[2])

        self.bu_br_l2 = nn.Sequential(nn.BatchNorm2d(dec_planes[0]),
                                      nn.PReLU(dec_planes[0])
                                      )
        self.bu_br_l3 = nn.Sequential(nn.BatchNorm2d(dec_planes[1]),
                                      nn.PReLU(dec_planes[1])
                                      )
        self.bu_br_l4 = nn.Sequential(nn.BatchNorm2d(dec_planes[2]),
                                      nn.PReLU(dec_planes[2])
                                      )
        #=============================================================
        #                   SSSR BRANCH
        #=============================================================           
        self.bu_dec_l5 = Deconv_BN_ACT(dec_planes[3],dec_planes[3])
        self.bu_dec_l6 = Deconv_BN_ACT(dec_planes[3],dec_planes[3])

        self.init_params()

    def upsample(self, x):
        return F.interpolate(x, scale_factor=2, mode='bilinear', align_corners=True)

    def init_params(self):
        '''
        Function to initialze the parameters
        '''
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                init.kaiming_normal_(m.weight, mode='fan_out')
                if m.bias is not None:
                    init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                init.constant_(m.weight, 1)
                init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                init.normal_(m.weight, std=0.001)
                if m.bias is not None:
                    init.constant_(m.bias, 0)

    def get_basenet_params(self):
        modules_base = [self.base_net]
        for i in range(len(modules_base)):
            for m in modules_base[i].named_modules():
                if isinstance(m[1], nn.Conv2d) or isinstance(m[1], nn.BatchNorm2d) or isinstance(m[1], nn.PReLU):
                    for p in m[1].parameters():
                        if p.requires_grad:
                            yield p

    def get_segment_params(self):
        modules_seg = [self.bu_dec_l1, self.bu_dec_l2, self.bu_dec_l3, self.bu_dec_l4,
                       self.merge_enc_dec_l4, self.merge_enc_dec_l3, self.merge_enc_dec_l2,
                       self.bu_br_l4, self.bu_br_l3, self.bu_br_l2]
        for i in range(len(modules_seg)):
            for m in modules_seg[i].named_modules():
                if isinstance(m[1], nn.Conv2d) or isinstance(m[1], nn.BatchNorm2d) or isinstance(m[1], nn.PReLU):
                    for p in m[1].parameters():
                        if p.requires_grad:
                            yield p

    def forward(self, x):
        '''
        :param x: Receives the input RGB image
        :return: a C-dimensional vector, C=# of classes
        '''
        print(f"x.shape = {x.shape}")
        x_size = (x.size(2), x.size(3))

        ###### this model outputs: enc_out_l4
        enc_out_l1 = self.base_net.level1(x)  # 112
        if not self.base_net.input_reinforcement:
            del x
            x = None

        enc_out_l2 = self.base_net.level2_0(enc_out_l1, x)  # 56

        enc_out_l3_0 = self.base_net.level3_0(enc_out_l2, x)  # down-sample
        for i, layer in enumerate(self.base_net.level3):
            if i == 0:
                enc_out_l3 = layer(enc_out_l3_0)
            else:
                enc_out_l3 = layer(enc_out_l3)

        enc_out_l4_0 = self.base_net.level4_0(enc_out_l3, x)  # down-sample
        for i, layer in enumerate(self.base_net.level4):
            if i == 0:
                enc_out_l4 = layer(enc_out_l4_0)
            else:
                enc_out_l4 = layer(enc_out_l4)
        print(f"enc_out_l1.shape = {enc_out_l1.shape}")
        print(f"enc_out_l2.shape = {enc_out_l2.shape}")
        print(f"enc_out_l3.shape = {enc_out_l3.shape}")
        print(f"enc_out_l4.shape = {enc_out_l4.shape}")
        # bottom-up decoding
        ###### this model outputs: bu_out0
        bu_out = self.bu_dec_l1(enc_out_l4)
        print(f"bu_out0.shape = {bu_out.shape}")
        # Decoding block
        ###### this model outputs: bu_out1
        bu_out = self.upsample(bu_out)
        enc_out_l3_proj = self.merge_enc_dec_l2(enc_out_l3)
        bu_out = enc_out_l3_proj + bu_out
        bu_out = self.bu_br_l2(bu_out)
        bu_out = self.bu_dec_l2(bu_out)
        print(f"bu_out1.shape = {bu_out.shape}")
        #decoding block
        ###### this model outputs: bu_out2
        bu_out = self.upsample(bu_out)
        enc_out_l2_proj = self.merge_enc_dec_l3(enc_out_l2)
        bu_out = enc_out_l2_proj + bu_out
        bu_out = self.bu_br_l3(bu_out)
        bu_out = self.bu_dec_l3(bu_out)
        print(f"bu_out2.shape = {bu_out.shape}")
        # decoding block
        ###### this model outputs: bu_out
        bu_out = self.upsample(bu_out)
        enc_out_l1_proj = self.merge_enc_dec_l4(enc_out_l1)
        bu_out = enc_out_l1_proj + bu_out
        bu_out = self.bu_br_l4(bu_out)
        bu_out  = self.bu_dec_l4(bu_out)
        print(f"bu_out.shape = {bu_out.shape}")
        # return F.interpolate(bu_out, size=(x_size[0]*2, x_size[1]*2), mode='bilinear', align_corners=True)

        # sssr block
        sssr_1 = self.bu_dec_l5(bu_out)
        sssr_out = self.bu_dec_l6(sssr_1)
        print(f"sssr_1.shape = {sssr_1.shape}")
        print(f"sssr_out.shape = {sssr_out.shape}")

        return  sssr_out 

def espnetv2_seg(args):
    classes = args.classes
    scale=args.s
    dataset=args.dataset
    model = ESPNetv2Segmentation(args, classes=classes, dataset=dataset)
    return model



