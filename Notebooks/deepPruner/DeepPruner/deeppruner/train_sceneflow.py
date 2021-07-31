# ---------------------------------------------------------------------------
# DeepPruner: Learning Efficient Stereo Matching via Differentiable PatchMatch
#
# Copyright (c) 2019 Uber Technologies, Inc.
#
# Licensed under the Uber Non-Commercial License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at the root directory of this project. 
#
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Written by Shivam Duggal
# ---------------------------------------------------------------------------

from __future__ import print_function
import argparse
import os
import random
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
from torch.autograd import Variable
import numpy as np
from dataloader import kitti_collector as lt
from dataloader import kitti_loader as DA
from models.deeppruner import DeepPruner
from loss_evaluation import loss_evaluation
from tensorboardX import SummaryWriter
import skimage
import time
import logging
from models.config import config as config_args
from setup_logging import setup_logging

parser = argparse.ArgumentParser(description='DeepPruner')
#parser.add_argument('--datapath_monkaa', default='/',
#                    help='datapath for sceneflow monkaa dataset')
#parser.add_argument('--datapath_flying', default='/',
#                    help='datapath for sceneflow flying dataset')
#parser.add_argument('--datapath_driving', default='/',
#                    help='datapath for sceneflow driving dataset')
parser.add_argument('--epochs', type=int, default=100,
                    help='number of epochs to train')
parser.add_argument('--loadmodel', default='./weights/finetune_24.tar',
                    help='load model')
parser.add_argument('--save_dir', default='./',
                    help='save directory')
parser.add_argument('--savemodel', default='./weights/',
                    help='save model')
parser.add_argument('--logging_filename', default='./train_sceneflow.log',
                    help='save model')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')
parser.add_argument('--seed', type=int, default=1, metavar='S',
                    help='random seed (default: 1)')
parser.add_argument('--test', action='store_true', default=False,
                    help='test')
args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

torch.manual_seed(args.seed)
if args.cuda:
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = True


args.cost_aggregator_scale = config_args.cost_aggregator_scale
args.maxdisp = config_args.max_disp

setup_logging(args.logging_filename)


#all_left_img, all_right_img, all_left_disp, test_left_img, test_right_img, test_left_disp = #lt.datacollector('/notebooks/training',
#None , None )

#print(all_left_disp[0:5])
#print(all_left_img[0:5])
#print(len(all_left_disp))
#DA.KITTILoader(all_left_img, all_right_img, all_left_disp,True)
TrainImgLoader = torch.utils.data.DataLoader(
    DA.KITTILoader(True),
    batch_size=16, shuffle=True, num_workers=8, drop_last=False)
print(len(TrainImgLoader))

TestImgLoader = torch.utils.data.DataLoader(
    DA.KITTILoader(False),
    batch_size=1, shuffle=False, num_workers=4, drop_last=False)

model = DeepPruner()
writer = SummaryWriter()

if args.cuda:
    model = nn.DataParallel(model)
    model.cuda()


if args.loadmodel is not None:
    state_dict = torch.load(args.loadmodel)
    model.load_state_dict(state_dict['state_dict'], strict=True)
    print("Loading " + args.loadmodel + "\n")
logging.info('Number of model parameters: {}'.format(sum([p.data.nelement() for p in model.parameters()])))

optimizer = optim.Adam(model.parameters(), lr=0.001, betas=(0.9, 0.999))


def train(imgL, imgR, disp_L, iteration):
    model.train()
    imgL = Variable(torch.FloatTensor(imgL))
    imgR = Variable(torch.FloatTensor(imgR))
    disp_L = Variable(torch.FloatTensor(disp_L))

    if args.cuda:
        imgL, imgR, disp_true = imgL.cuda(), imgR.cuda(), disp_L.cuda()

    mask = disp_true < args.maxdisp
    mask.detach_()

    optimizer.zero_grad()
    result = model(imgL, imgR)

    loss, _ = loss_evaluation(result, disp_true, mask, args.cost_aggregator_scale)

    loss.backward()
    optimizer.step()

    return loss.item()


def test(imgL,imgR,disp_L,iteration):

        model.eval()
        with torch.no_grad():
            imgL   = Variable(torch.FloatTensor(imgL))
            imgR   = Variable(torch.FloatTensor(imgR))   
            disp_L = Variable(torch.FloatTensor(disp_L))

            if args.cuda:
                imgL, imgR, disp_true = imgL.cuda(), imgR.cuda(), disp_L.cuda()

            mask = (disp_true > 0)
            mask.detach_()
            
            optimizer.zero_grad()
            
            result = model(imgL,imgR)
            loss, output_disparity = loss_evaluation(result, disp_true, mask, args.cost_aggregator_scale)

            #computing 3-px error: (source psmnet)#
            true_disp = disp_true.data.cpu()
            disp_true = true_disp
            pred_disp = output_disparity.data.cpu()

            index = np.argwhere(true_disp>0)
            disp_true[index[0][:], index[1][:], index[2][:]] = np.abs(true_disp[index[0][:], index[1][:], index[2][:]]-pred_disp[index[0][:], index[1][:], index[2][:]])
            correct = (disp_true[index[0][:], index[1][:], index[2][:]] < 3)|(disp_true[index[0][:], index[1][:], index[2][:]] < true_disp[index[0][:], index[1][:], index[2][:]]*0.05)      
            torch.cuda.empty_cache()             
            
            loss = 1-(float(torch.sum(correct))/float(len(index[0])))
                
        return loss

def adjust_learning_rate(optimizer, epoch):
    if epoch <= 20:
        lr = 0.001
    elif epoch <= 40:
        lr = 0.0007
    elif epoch <= 60:
        lr = 0.0003
    else:
        lr = 0.0001
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def main():
    if(args.test==False):
        for epoch in range(25, args.epochs):
            total_train_loss = 0
            total_test_loss = 0
            total_epe_loss = 0
            adjust_learning_rate(optimizer, epoch)
    
    #        if epoch % 1 == 0 and epoch != 0:
    #            logging.info("testing...")
    #            for batch_idx, (imgL, imgR, disp_L, pad_w, pad_h) in enumerate(TestImgLoader):
    #                start_time = time.time()
    #                test_loss, epe_loss = test(imgL, imgR, disp_L, batch_idx, int(pad_w[0].item()), int(pad_h[0].item()))
    #                total_test_loss += test_loss
    #                total_epe_loss += epe_loss
    #
    #                logging.info('Iter %d 3-px error in val = %.3f, time = %.2f \n' %
    #                      (batch_idx, epe_loss, time.time() - start_time))
    #
    #                writer.add_scalar("val-loss-iter", test_loss, epoch * 4370 + batch_idx)
    #                writer.add_scalar("val-epe-loss-iter", epe_loss, epoch * 4370 + batch_idx)
    #
    #            logging.info('epoch %d total test loss = %.3f' % (epoch, total_test_loss / len(TestImgLoader)))
    #            writer.add_scalar("val-loss", total_test_loss / len(TestImgLoader), epoch)
    #            logging.info('epoch %d total epe loss = %.3f' % (epoch, total_epe_loss / len(TestImgLoader)))
    #            writer.add_scalar("epe-loss", total_epe_loss / len(TestImgLoader), epoch)
    
            for batch_idx, (imgL_crop, imgR_crop, disp_crop_L) in enumerate(TrainImgLoader):
                start_time = time.time()
                loss = train(imgL_crop, imgR_crop, disp_crop_L, batch_idx)
                total_train_loss += loss
    
                writer.add_scalar("loss-iter", loss, batch_idx + len(TrainImgLoader) * epoch)
                logging.info('Iter %d training loss = %.3f , time = %.2f \n' % (batch_idx, loss, time.time() - start_time))
    
            logging.info('epoch %d total training loss = %.3f' % (epoch, total_train_loss / len(TrainImgLoader)))
            writer.add_scalar("loss", total_train_loss / len(TrainImgLoader), epoch)
    
            # SAVE
            if epoch % 1 == 0:
                savefilename = args.savemodel + 'finetune_' + str(epoch) + '.tar'
                torch.save({
                    'epoch': epoch,
                    'state_dict': model.state_dict(),
                    'train_loss': total_train_loss,
                    'test_loss': total_test_loss,
                }, savefilename)
        else:
            logging.info("testing...")
            for batch_idx, (imgL, imgR, disp_L) in enumerate(TestImgLoader):
                test_loss = test(imgL,imgR,disp_L,batch_idx)
                total_test_loss += test_loss
                logging.info('Iter %d 3-px error in val = %.3f \n' %(batch_idx, test_loss))
                
            logging.info('epoch %d total test loss = %.3f' %(epoch, total_test_loss/len(TestImgLoader)))
            #writer.add_scalar("val-loss",total_test_loss/len(TestImgLoader),epoch)


if __name__ == '__main__':
    main()
