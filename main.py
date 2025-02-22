import sys
import random
import glob
import os
from datetime import datetime
from copy import deepcopy
from tqdm import tqdm
from skimage.segmentation import mark_boundaries
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
import torch
from torch.utils.tensorboard import SummaryWriter
from torch.utils.data import DataLoader
from torch import optim
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torchvision.transforms.functional import to_pil_image
from torchvision import transforms
from torchvision.utils import save_image

from net.Unet import Unet
from net.FCN import fcn
from utils.utils import collect_batch, get_lr, loss_epoch, loss_func, ROCMetric, PD_FA
from utils.dataloader import InfraredDataset

class App(object):
    def __init__(self, model_type='unet'):
        super().__init__()
        self.set_seed()  # 设置随机数种子保证实验一致性
        self.model_type = model_type
        self.model = self.get_model()
        self.init_epoch = 0  # 初始步数
        self.epochs = 40  # 训练总轮数
        self.ckpt = '%s_best.pt' % self.model_type  # 预训练模型保存位置
        self.pred_train = True
        self.dataset_dir = './sirst'
        self.train_index = open('./sirst/idx_320/train.txt').readlines()
        self.test_index = open('./sirst/idx_320/test.txt').readlines()
        self.batch_size = 64
        self.train_dl, self.test_dl = self.generate_ds()
        self.optimizer = optim.Adam(self.model.parameters(), lr=3e-4)
        self.lr_stp = ReduceLROnPlateau(
        self.optimizer, mode='min', factor=0.5, patience=5, verbose=1)
        self.roc = ROCMetric(1, 10)
        self.pd_fa = PD_FA(1, 10)
        self.tb_writer = SummaryWriter('tb_logs/%s' % self.time_ink())
        self.infer_dir = 'pred'
        torch.cuda.empty_cache()

    def train(self):
        if torch.cuda.is_available():
            self.model = self.model.cuda()

        if self.pred_train:
            self.model.load_state_dict(torch.load(self.ckpt))

        best_wts = deepcopy(self.model.state_dict())
        best_loss = float('inf')

        for epoch in range(self.init_epoch, self.epochs):
            current_lr = get_lr(self.optimizer)
            self.model.train()
            train_loss, train_metric = loss_epoch(
                epoch, self.model, loss_func, self.train_dl, sanity_check=False, opt=self.optimizer)

            self.model.eval()
            with torch.no_grad():
                val_loss, val_metric = loss_epoch(
                    epoch, self.model, loss_func, self.train_dl, sanity_check=False, opt=None, roc=self.roc)

            if val_loss < best_loss:
                best_loss = val_loss
                best_wts = deepcopy(self.model.state_dict())
                print("Save Best Model")
                torch.save(self.model.state_dict(), self.ckpt)

            self.tb_writer.add_scalar('train_loss', train_loss, epoch)
            self.tb_writer.add_scalar('train_metric', train_metric, epoch)
            self.tb_writer.add_scalar('val_loss', val_loss, epoch)
            self.tb_writer.add_scalar('val_metric', val_metric, epoch)

            self.lr_stp.step(val_loss)

            if current_lr != get_lr(self.optimizer):
                self.model.load_state_dict(best_wts)
                print("Load Best Model")

    def evaluate(self):
        self.model.load_state_dict(torch.load(self.ckpt))
        if torch.cuda.is_available():
            self.model = self.model.cuda()
        test_ds = InfraredDataset(self.test_index)
        test_dl = DataLoader(test_ds, batch_size=1,
                             shuffle=False, collate_fn=collect_batch)
        for i, (xb, yb) in enumerate(tqdm(test_dl)):
            if torch.cuda.is_available():
                xb = xb.cuda()
                yb = yb.cuda()
            output = self.model(xb)
            save_image([output[0], xb[0], yb[0]], f'{self.infer_dir}/pred_%d.jpg' % i)
            self.pd_fa.update(output.cpu().detach().numpy(),
                              yb.cpu().detach().numpy())

        FA, PD = self.pd_fa.get(img_num=len(test_ds))
        for i in range(10):
            with open('%s_result_.txt' % self.model_type, 'a') as f:
                if i == 0:
                    f.write('%s \n' % self.time_ink())
                info = "FA %.6f PD %.6f \n" % (
                    FA[i],
                    PD[i]
                )
                f.write(info)
        print('Probablity of detection %.3f %% False-alarm ratio %.3f %% ' %
              (PD[0] * 100, FA[0] * 100))

    def test(self, infer_dir):
        self.model.load_state_dict(torch.load(self.ckpt))
        if torch.cuda.is_available():
            self.model = self.model.cuda()

        try:
            save_dir = infer_dir+'_pred'
            os.makedirs(save_dir)
        except:
            pass

        TF = transforms.Compose([
            transforms.Grayscale(),
            transforms.Resize((256, 256)),
            transforms.ToTensor(),
        ])

        infer_imgs = glob.glob('%s/*.png' % infer_dir)

        for path in tqdm(infer_imgs):
            image = Image.open(path)
            tensor_img = TF(image)
            tensor_img = torch.unsqueeze(tensor_img, 0)
            if torch.cuda.is_available():
                tensor_img = tensor_img.cuda()
            pred = self.model(tensor_img)
            save_image(pred, path.replace(infer_dir, save_dir))

    def generate_ds(self):
        train_ds = InfraredDataset(self.dataset_dir, self.train_index)
        test_ds = InfraredDataset(self.dataset_dir, self.test_index)
        train_dl = DataLoader(
            train_ds, batch_size=self.batch_size, shuffle=True, collate_fn=collect_batch, num_workers=8)
        test_dl = DataLoader(test_ds, batch_size=self.batch_size,
                             shuffle=False, collate_fn=collect_batch, num_workers=8)
        return train_dl, test_dl

    def vis_dl(self):

        for i, (batch_image, batch_label) in enumerate(self.train_dl):
            image, label = batch_image[0], batch_label[0]
            image, label = to_pil_image(image), to_pil_image(label)
            image, label = np.array(image), np.array(label)
            print(image.shape, label.shape)
            vis = mark_boundaries(image, label, color=(1, 1, 0))
            image, label = np.stack([image] * 3, -1), np.stack([label] * 3, -1)
            plt.imsave('train_image_%d.png' % i, vis)
            break

        for i, (batch_image, batch_label) in enumerate(self.test_dl):
            image, label = batch_image[0], batch_label[0]
            image, label = to_pil_image(image), to_pil_image(label)
            image, label = np.array(image), np.array(label)
            print(image.shape, label.shape)
            vis = mark_boundaries(image, label, color=(1, 1, 0))
            image, label = np.stack([image] * 3, -1), np.stack([label] * 3, -1)
            plt.imsave('test_image_%d.png' % i, vis)
            break

    @staticmethod
    def set_seed():
        torch.manual_seed(1024)
        np.random.seed(1024)
        random.seed(1024)

    @staticmethod
    def time_ink():
        now = datetime.now()
        return now.strftime("%d_%m_%Y_%H:%M:%S")

    def get_model(self):
        if self.model_type in ["unet", "fcn"]:
            if self.model_type == 'unet':
                return Unet(1)
            elif self.model_type == 'fcn':
                return fcn(1)
        else:
            ValueError("Now only sport Unet FCN")


if __name__ == "__main__":

    type = sys.argv[1]

    application = App()

    if type == 'train':
        print("Strat Train Total Epoch %s" % application.epochs)
        application.train()
    elif type == 'test':
        print('start test ')
        path  = sys.argv[2]
        application.test(path)
    elif type == 'evaluate':
        print("Strat evaluate ")
        application.evaluate()
    elif type == 'vis_dl':
        application.vis_dl()
    else:
        ValueError("No Match Command!")
