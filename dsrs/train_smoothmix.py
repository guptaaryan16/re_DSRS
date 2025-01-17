"""
From https://github.com/jh-jeong/smoothmix/blob/main/code/train.py
"""

import argparse
import time
from typing import Optional

import math
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import Optimizer
from torch.utils.data import DataLoader

from architectures import ARCHITECTURES
from datasets import DATASETS, get_num_classes
from train_utils import AverageMeter, log, requires_grad_, accuracy
from train_utils import prologue

from distribution import StandardGaussian, GeneralGaussian, LinftyGaussian, LinftyGeneralGaussian, L1GeneralGaussian


def init_distribution(k, d, noise_sd):
    if k == 0:
        return StandardGaussian(d, noise_sd)
    else:
        return GeneralGaussian(d, k, noise_sd)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('dataset', type=str, choices=DATASETS)
parser.add_argument('arch', type=str, choices=ARCHITECTURES)
parser.add_argument('--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=150, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--batch', default=256, type=int, metavar='N',
                    help='batchsize (default: 256)')
parser.add_argument('--lr', '--learning-rate', default=0.1, type=float,
                    help='initial learning rate', dest='lr')
parser.add_argument('--lr_step_size', type=int, default=50,
                    help='How often to decrease learning by gamma.')
parser.add_argument('--gamma', type=float, default=0.1,
                    help='LR is multiplied by gamma on schedule.')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--noise_sd', default=0.0, type=float,
                    help="standard deviation of Gaussian noise for data augmentation")
parser.add_argument('--print-freq', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--id', default=None, type=int,
                    help='experiment id, `randint(10000)` if None')

#####################
# Options added by Salman et al. (2019)
parser.add_argument('--resume', action='store_true',
                    help='if true, tries to resume training from existing checkpoint')
parser.add_argument('--pretrained-model', type=str, default='',
                    help='Path to a pretrained model')

#####################
parser.add_argument('--num-noise-vec', default=1, type=int,
                    help="number of noise vectors. `m` in the paper.")
parser.add_argument('--alpha', default=1.0, type=float, help="step-size for adversarial attacks.")
parser.add_argument('--num-steps', default=8, type=int,
                    help="number of attack updates. `T` in the paper.")
parser.add_argument('--eta', default=1.0, type=float,
                    help="hyperparameter to control the relative strength of the mixup loss.")

parser.add_argument('--mix_step', default=0, type=int,
                    help="which sample to use for the clean side. `1` means to use of one-step adversary.")
parser.add_argument('--maxnorm_s', default=None, type=float)
parser.add_argument('--maxnorm', default=None, type=float)
parser.add_argument('--warmup', default=10, type=int)

parser.add_argument('--k', default=0, type=int, help="Final general Gaussian parameter")
parser.add_argument('--k-warmup', default=100, type=int, help="Number of epochs over which the general Gaussian "
                                                              "parameter increases from zero to desired k")
parser.add_argument('--save_path', default='no_name', type=str, help="path_where_model_to_be_saved")

args = parser.parse_args()
mode = f"smix_{args.alpha}_{args.num_steps}_m{args.mix_step}"
if args.maxnorm_s:
    mode += f'_ms{args.maxnorm_s}'
if args.maxnorm:
    mode += f'_max{args.maxnorm}'
args.outdir = f"logs/{args.dataset}/{mode}/k_{args.k}_{args.k_warmup}/eta_{args.eta}/num_{args.num_noise_vec}/noise_{args.noise_sd}"


def main():
    train_loader, test_loader, criterion, model, optimizer, scheduler, \
    starting_epoch, logfilename, model_path, device, writer = prologue(args)

    args.n_classes = get_num_classes(args.dataset)
    if args.maxnorm_s is None:
        args.maxnorm_s = args.alpha * args.mix_step
    attacker = SmoothMix_PGD(steps=args.num_steps, mix_step=args.mix_step,
                             alpha=args.alpha, maxnorm=args.maxnorm, maxnorm_s=args.maxnorm_s)

    step_counter = {'step': 0}

    for epoch in range(starting_epoch, args.epochs):
        args.warmup_v = np.min([1.0, (epoch + 1) / args.warmup])
        attacker.maxnorm_s = args.warmup_v * args.maxnorm_s

        if args.dataset != 'imagenet':
            if args.k == 0:
                now_k = 0
            else:
                now_k = math.ceil(args.k - args.k * math.exp(- epoch * math.log(args.k) / args.k_warmup)) \
                    if epoch <= args.k_warmup else args.k
            print(f'Epoch {epoch} with k = {now_k}')

        before = time.time()
        if args.dataset != 'imagenet':
            train_loss = train(train_loader, model, optimizer, epoch, now_k,
                               args.noise_sd, attacker, device, writer)
        else:
            train_loss = train(train_loader, model, optimizer, epoch, args.k,
                               args.noise_sd, attacker, device, writer, args.k_warmup, step_counter)
        if args.dataset != 'imagenet':
            test_loss, test_acc = test(test_loader, model, criterion, epoch, now_k, args.noise_sd, device, writer, args.print_freq)
        else:
            if args.k == 0:
                now_k = 0
            else:
                now_k = math.ceil(args.k - args.k * math.exp(- step_counter['step'] * math.log(args.k) / args.k_warmup)) \
                    if step_counter['step'] <= args.k_warmup else args.k
            test_loss, test_acc = test(test_loader, model, criterion, epoch, now_k,
                                       args.noise_sd, device, writer, args.print_freq)
        after = time.time()

        log(logfilename, "{}\t{:.3}\t{:.3}\t{:.3}\t{:.3}\t{:.3}\t{:.3}".format(
            epoch, after - before,
            scheduler.get_lr()[0], train_loss, 0.0, test_loss, test_acc))

        # In PyTorch 1.1.0 and later, you should call `optimizer.step()` before `lr_scheduler.step()`.
        # See more details at https://pytorch.org/docs/stable/optim.html#how-to-adjust-learning-rate
        scheduler.step(epoch)

        torch.save({
            'epoch': epoch + 1,
            'arch': args.arch,
            'state_dict': model.state_dict(),
            'optimizer': optimizer.state_dict(),
        }, args.save_path)


def _chunk_minibatch(batch, num_batches):
    X, y = batch
    batch_size = len(X) // num_batches
    for i in range(num_batches):
        yield X[i*batch_size : (i+1)*batch_size], y[i*batch_size : (i+1)*batch_size]


def _mixup_data(x1, x2, y1, n_classes):
    '''Returns mixed inputs, pairs of targets, and lambda'''
    device = x1.device

    _eye = torch.eye(n_classes, device=device)
    _unif = _eye.mean(0, keepdim=True)
    lam = torch.rand(x1.size(0), device=device) / 2

    mixed_x = (1 - lam).view(-1, 1, 1, 1) * x1 + lam.view(-1, 1, 1, 1) * x2
    mixed_y = (1 - lam).view(-1, 1) * y1 + lam.view(-1, 1) * _unif

    return mixed_x, mixed_y


def _avg_softmax(logits):
    m = len(logits)
    softmax = [F.softmax(logit, dim=1) for logit in logits]
    avg_softmax = sum(softmax) / m
    return avg_softmax


def train(loader: DataLoader, model, optimizer: Optimizer, epoch: int, now_k:int, noise_sd: float,
          attacker, device: torch.device, writer=None, k_warmup=None, step_counter=None):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    losses_reg = AverageMeter()
    end = time.time()

    # switch to train mode
    model.train()
    requires_grad_(model, True)

    step_c = step_counter['step'] if step_counter is not None else None
    k_lim = now_k if step_counter is not None else None

    for i, batch in enumerate(loader):
        distribution = None

        # measure data loading time
        data_time.update(time.time() - end)

        if step_c is not None:
            # init real k then
            if k_lim == 0:
                now_k = 0
            else:
                now_k = math.ceil(k_lim - k_lim * math.exp(- step_c * math.log(k_lim) / k_warmup)) if step_c <= k_warmup else k_lim
            step_c += 1
            step_counter['step'] += 1

        mini_batches = _chunk_minibatch(batch, args.num_noise_vec)
        for inputs, targets in mini_batches:
            inputs, targets = inputs.to(device), targets.to(device)
            batch_size = inputs.size(0)

            if distribution is None:
                d = inputs.numel() // batch_size
                distribution = init_distribution(now_k, d, noise_sd)

            noises = [torch.tensor(distribution.sample(batch_size).astype(np.float32), device=device).reshape_as(inputs)
                      for _ in range(args.num_noise_vec)]

            # noises = [torch.randn_like(inputs) * noise_sd for _ in range(args.num_noise_vec)]

            requires_grad_(model, False)
            model.eval()
            inputs, inputs_adv = attacker.attack(model, inputs, targets, noises=noises)
            model.train()
            requires_grad_(model, True)

            in_clean_c = torch.cat([inputs + noise for noise in noises], dim=0)
            logits_c = model(in_clean_c)
            targets_c = targets.repeat(args.num_noise_vec)

            logits_c_chunk = torch.chunk(logits_c, args.num_noise_vec, dim=0)
            clean_avg_sm = _avg_softmax(logits_c_chunk)

            loss_xent = F.cross_entropy(logits_c, targets_c, reduction='none')

            in_mix, targets_mix = _mixup_data(inputs, inputs_adv, clean_avg_sm, args.n_classes)

            in_mix_c = torch.cat([in_mix + noise for noise in noises], dim=0)
            targets_mix_c = targets_mix.repeat(args.num_noise_vec, 1)
            logits_mix_c = F.log_softmax(model(in_mix_c), dim=1)

            _, top1_idx = torch.topk(clean_avg_sm, 1)
            ind_correct = (top1_idx[:, 0] == targets).float()
            ind_correct = ind_correct.repeat(args.num_noise_vec)

            loss_mixup = F.kl_div(logits_mix_c, targets_mix_c, reduction='none').sum(1)
            loss = loss_xent.mean() + args.eta * args.warmup_v * (ind_correct * loss_mixup).mean()

            losses.update(loss.item(), batch_size)

            # compute gradient and do SGD step
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print('k =', now_k, 'Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.avg:.3f}\t'
                  'Data {data_time.avg:.3f}\t'
                  'Loss {loss.avg:.4f}\t'.format(
                epoch, i, len(loader), batch_time=batch_time,
                data_time=data_time, loss=losses))

    writer.add_scalar('loss/train', losses.avg, epoch)
    writer.add_scalar('loss/adv', losses_reg.avg, epoch)
    writer.add_scalar('batch_time', batch_time.avg, epoch)

    return losses.avg


def test(loader, model, criterion, epoch, now_k, noise_sd, device, writer=None, print_freq=10):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()
    end = time.time()

    # switch to eval mode
    model.eval()

    with torch.no_grad():
        distribution = None
        for i, (inputs, targets) in enumerate(loader):

            if distribution is None:
                batch_size = inputs.size(0)
                d = inputs.numel() // batch_size
                distribution = init_distribution(now_k, d, noise_sd)

            # measure data loading time
            data_time.update(time.time() - end)

            inputs, targets = inputs.to(device), targets.to(device)

            # augment inputs with noise
            noise = distribution.sample(inputs.size(0)).astype(np.float32)
            noise = torch.tensor(noise, device=device).reshape_as(inputs)
            inputs = inputs + noise
            # inputs = inputs + torch.randn_like(inputs, device=device) * noise_sd

            # compute output
            outputs = model(inputs)
            loss = criterion(outputs, targets)

            # measure accuracy and record loss
            acc1, acc5 = accuracy(outputs, targets, topk=(1, 5))
            losses.update(loss.item(), inputs.size(0))
            top1.update(acc1.item(), inputs.size(0))
            top5.update(acc5.item(), inputs.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % print_freq == 0:
                print('Test: [{0}/{1}]\t'
                      'Time {batch_time.avg:.3f}\t'
                      'Data {data_time.avg:.3f}\t'
                      'Loss {loss.avg:.4f}\t'
                      'Acc@1 {top1.avg:.3f}\t'
                      'Acc@5 {top5.avg:.3f}'.format(
                    i, len(loader), batch_time=batch_time, data_time=data_time,
                    loss=losses, top1=top1, top5=top5))

        if writer:
            writer.add_scalar('loss/test', losses.avg, epoch)
            writer.add_scalar('accuracy/test@1', top1.avg, epoch)
            writer.add_scalar('accuracy/test@5', top5.avg, epoch)

        return (losses.avg, top1.avg)


class SmoothMix_PGD(object):
    def __init__(self,
                 steps: int,
                 mix_step: int,
                 alpha: Optional[float] = None,
                 maxnorm_s: Optional[float] = None,
                 maxnorm: Optional[float] = None) -> None:
        super(SmoothMix_PGD, self).__init__()
        self.steps = steps
        self.mix_step = mix_step
        self.alpha = alpha
        self.maxnorm = maxnorm
        if maxnorm_s is None:
            self.maxnorm_s = alpha * mix_step
        else:
            self.maxnorm_s = maxnorm_s

    def attack(self, model, inputs, labels, noises=None):
        if inputs.min() < 0 or inputs.max() > 1: raise ValueError('Input values should be in the [0, 1] range.')

        def _batch_l2norm(x):
            x_flat = x.reshape(x.size(0), -1)
            return torch.norm(x_flat, dim=1)

        def _project(x, x0, maxnorm=None):
            if maxnorm is not None:
                eta = x - x0
                eta = eta.renorm(p=2, dim=0, maxnorm=maxnorm)
                x = x0 + eta
            x = torch.clamp(x, 0, 1)
            x = x.detach()
            return x

        adv = inputs.detach()
        init = inputs.detach()
        for i in range(self.steps):
            if i == self.mix_step:
                init = adv.detach()
            adv.requires_grad_()

            softmax = [F.softmax(model(adv + noise), dim=1) for noise in noises]
            avg_softmax = sum(softmax) / len(noises)
            logsoftmax = torch.log(avg_softmax.clamp(min=1e-20))

            loss = F.nll_loss(logsoftmax, labels, reduction='sum')

            grad = torch.autograd.grad(loss, [adv])[0]
            grad_norm = _batch_l2norm(grad).view(-1, 1, 1, 1)
            grad = grad / (grad_norm + 1e-8)
            adv = adv + self.alpha * grad

            adv = _project(adv, inputs, self.maxnorm)
        init = _project(init, inputs, self.maxnorm_s)

        return init, adv


if __name__ == "__main__":
    main()
