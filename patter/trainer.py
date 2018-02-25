import math
import time
import torch
from tqdm import tqdm as tqdm_wrap
from torch.utils.data import DataLoader
from marshmallow.exceptions import ValidationError
from .config import TrainerConfiguration
from .decoder import GreedyCTCDecoder
from .data import BucketingSampler, audio_seq_collate_fn
from .util import AverageMeter
from .models import SpeechModel


class Trainer(object):
    def __init__(self, train_config, tqdm=False):
        self.cfg = train_config['trainer']
        self.output = train_config['output']
        self.cuda = train_config['cuda']
        self.train_id = train_config['expt_id']
        self.tqdm=tqdm
        self.max_norm = self.cfg.get('max_norm', None)

    def train(self, model, corpus, eval=None):
        # set up data loaders
        train_sampler = BucketingSampler(corpus, batch_size=self.cfg['batch_size'])
        train_loader = DataLoader(corpus, num_workers=self.cfg['num_workers'], collate_fn=audio_seq_collate_fn,
                                  pin_memory=True, batch_sampler=train_sampler)
        if eval is not None:
            eval_loader = DataLoader(eval, num_workers=self.cfg['num_workers'], collate_fn=audio_seq_collate_fn,
                                     pin_memory=True, batch_size=self.cfg['batch_size'])
        else:
            eval_loader = None

        if self.cuda:
            model = model.cuda()

        print(model)

        # set up optimizer
        opt_cfg = self.cfg['optimizer']
        optimizer = torch.optim.SGD(model.parameters(), lr=opt_cfg['learning_rate'],
                                    momentum=opt_cfg['momentum'], nesterov=opt_cfg['use_nesterov'])
        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1, gamma=opt_cfg['lr_annealing'])

        # primary training loop
        best_wer = math.inf

        for epoch in range(self.cfg['epochs']):
            # adjust lr
            scheduler.step()
            # print("> Learning rate annealed to {0:.6f}".format(scheduler.get_lr()[0]))
            
            avg_loss = self.train_epoch(train_loader, model, optimizer, epoch)
            print("Epoch {} Summary:".format(epoch))
            print('    Train:\tAverage Loss {loss:.3f}\t'.format(loss=avg_loss))

            avg_wer, avg_cer = validate(eval_loader, model)
            print('    Validation:\tAverage WER {wer:.3f}\tAverage CER {cer:.3f}'
                  .format(wer=avg_wer, cer=avg_cer))

            if avg_wer < best_wer:
                best_wer = avg_wer
                # print("Better model found. Saving.")
                torch.save(SpeechModel.serialize(model, optimizer=optimizer), self.output['model_path'])

    def train_epoch(self, train_loader, model, optimizer, epoch):
        model.train()

        batch_time = AverageMeter()
        data_time = AverageMeter()
        losses = AverageMeter()

        loader = train_loader
        if self.tqdm:
            loader = tqdm_wrap(loader, desc="Epoch {}".format(epoch), leave=False)

        end = time.time()
        for i, data in enumerate(loader):
            # measure data loading time
            data_time.update(time.time() - end)

            # create variables
            feat, target, feat_len, target_len = tuple(torch.autograd.Variable(i, requires_grad=False) for i in data)
            if self.cuda:
                feat = feat.cuda()

            # compute output
            # feat is (batch, 1,  feat_dim,  seq_len)
            # output is (seq_len, batch, output_dim)
            output, output_len = model(feat, feat_len)
            loss = model.loss(output, target, output_len, target_len)

            # munge the loss
            avg_loss = loss.data.sum() / feat.size(0)  # average the loss by minibatch
            inf = math.inf
            if avg_loss == inf or avg_loss == -inf:
                print("WARNING: received an inf loss, setting loss value to 0")
                avg_loss = 0
            losses.update(avg_loss, feat.size(0))

            # compute gradient
            optimizer.zero_grad()
            loss.backward()
            if self.max_norm:
                torch.nn.utils.clip_grad_norm(model.parameters(), self.max_norm)
            optimizer.step()

            del loss
            del output
            del output_len

            # measure time taken
            batch_time.update(time.time() - end)
            end = time.time()

            if not self.tqdm:
                print('Epoch: [{0}][{1}/{2}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t'.format((epoch + 1), (i + 1), len(train_loader),
                                                                      batch_time=batch_time, data_time=data_time,
                                                                      loss=losses))
            else:
                loader.set_postfix(loss=losses.val)
        return losses.avg

    @classmethod
    def load(cls, trainer_config, tqdm=False):
        try:
            cfg = TrainerConfiguration().load(trainer_config)
        except ValidationError as err:
            print(err.messages)
            raise err
        return cls(cfg.data, tqdm=tqdm)


def split_targets(targets, target_sizes):
    results = []
    offset = 0
    for size in target_sizes:
        results.append(targets[offset:offset + size])
        offset += size
    return results


def validate(val_loader, model, decoder=None, tqdm=True):
    if decoder is None:
        decoder = GreedyCTCDecoder(model.labels)
    batch_time = AverageMeter()

    model.eval()

    loader = tqdm_wrap(val_loader, desc="Validate", leave=False) if tqdm else val_loader

    end = time.time()
    wer, cer = 0.0, 0.0
    for i, data in enumerate(loader):
        # create variables
        feat, target, feat_len, target_len = tuple(torch.autograd.Variable(i, volatile=True) for i in data)
        if model.is_cuda:
            feat = feat.cuda()

        # compute output
        output, output_len = model(feat, feat_len)

        # do the decode
        decoded_output, _ = decoder.decode(output.transpose(0, 1).data, output_len.data)
        target_strings = decoder.convert_to_strings(split_targets(target.data, target_len.data))
        for x in range(len(target_strings)):
            transcript, reference = decoded_output[x][0], target_strings[x][0]
            wer += decoder.wer(transcript, reference) / float(len(reference.split()))
            cer += decoder.cer(transcript, reference) / float(len(reference))

        del output
        del output_len

        # measure time taken
        batch_time.update(time.time() - end)
        end = time.time()
    wer = wer * 100 / len(val_loader.dataset)
    cer = cer * 100 / len(val_loader.dataset)

    return wer, cer
