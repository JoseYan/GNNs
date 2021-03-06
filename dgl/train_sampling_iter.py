import argparse
import os
import time
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
from sampler import SAINTNodeSampler, SAINTEdgeSampler, SAINTRandomWalkSampler
from config import CONFIG
from modules import GCNNet
from utils import Logger, evaluate, save_log_dir, load_data, calc_f1
import warnings
from tqdm import tqdm
import numpy as np

def main(args, task):
    warnings.filterwarnings('ignore')
    multilabel_data = {'ppi', 'yelp', 'amazon'}
    multilabel = args.dataset in multilabel_data

    # This flag is excluded for too large dataset, like amazon, the graph of which is too large to be directly
    # shifted to one gpu. So we need to
    # 1. put the whole graph on cpu, and put the subgraphs on gpu in training phase
    # 2. put the model on gpu in training phase, and put the model on cpu in validation/testing phase
    # We need to judge cpu_flag and cuda (below) simultaneously when shift model between cpu and gpu
    if args.dataset in ['amazon']:
        cpu_flag = True
    else:
        cpu_flag = False

    # load and preprocess dataset
    data = load_data(args, multilabel)
    g = data.g
    train_mask = g.ndata['train_mask']
    val_mask = g.ndata['val_mask']
    test_mask = g.ndata['test_mask']
    labels = g.ndata['label']

    train_nid = data.train_nid

    in_feats = g.ndata['feat'].shape[1]
    n_classes = data.num_classes
    n_nodes = g.num_nodes()
    n_edges = g.num_edges()

    n_train_samples = train_mask.int().sum().item()
    n_val_samples = val_mask.int().sum().item()
    n_test_samples = test_mask.int().sum().item()

    print("""----Data statistics------'
    #Nodes %d
    #Edges %d
    #Classes/Labels (multi binary labels) %d
    #Train samples %d
    #Val samples %d
    #Test samples %d""" %
          (n_nodes, n_edges, n_classes,
           n_train_samples,
           n_val_samples,
           n_test_samples))
    # load sampler

    kwargs = {
        'dn': args.dataset, 'g': g, 'train_nid': train_nid, 'num_workers_sampler': args.num_workers_sampler,
        'num_subg_sampler': args.num_subg_sampler, 'batch_size_sampler': args.batch_size_sampler,
        'online': args.online, 'num_subg': args.num_subg, 'full': args.full
    }

    if args.sampler == "node":
        saint_sampler = SAINTNodeSampler(args.node_budget, **kwargs)
    elif args.sampler == "edge":
        saint_sampler = SAINTEdgeSampler(args.edge_budget, **kwargs)
    elif args.sampler == "rw":
        saint_sampler = SAINTRandomWalkSampler(args.num_roots, args.length, **kwargs)
    else:
        raise NotImplementedError
    loader = DataLoader(saint_sampler, collate_fn=saint_sampler.__collate_fn__, batch_size=1,
                        shuffle=True, num_workers=args.num_workers, drop_last=False)
    # set device for dataset tensors
    if args.gpu < 0:
        cuda = False
    else:
        cuda = True
        torch.cuda.set_device(args.gpu)
        val_mask = val_mask.cuda()
        test_mask = test_mask.cuda()
        if not cpu_flag:
            g = g.to('cuda:{}'.format(args.gpu))

    print('labels shape:', g.ndata['label'].shape)
    print("features shape:", g.ndata['feat'].shape)

    model = GCNNet(
        in_dim=in_feats,
        hid_dim=args.n_hidden,
        out_dim=n_classes,
        arch=args.arch,
        dropout=args.dropout,
        batch_norm=not args.no_batch_norm,
        aggr='mean'
    )
    print(model)

    if cuda:
        model.cuda()

    interval = g.num_nodes() // args.node_budget
    print('Eval interval ', interval)
    # logger and so on
    log_dir = save_log_dir(args)
    logger = Logger(os.path.join(log_dir, 'loggings'))
    logger.write(args)

    # use optimizer
    optimizer = torch.optim.Adam(model.parameters(),
                                 lr=args.lr)

    # set train_nids to cuda tensor
    if cuda:
        if isinstance(train_nid, np.ndarray):
            train_nid = torch.from_numpy(train_nid).cuda()
        else:
            train_nid = train_nid.cuda()
        print("GPU memory allocated before training(MB)",
              torch.cuda.memory_allocated(device=train_nid.device) / 1024 / 1024)
    start_time = time.time()
    best_f1 = -1

    dur = []
    dd = []
    for epoch in range(100):
        ep_start = time.time()
        ev = []
        print('{} subgraphs'.format(len(loader)))
        for j, subg in tqdm(enumerate(loader)):
            iter_start = time.time()
            if cuda:
                subg = subg.to(torch.cuda.current_device())
                #print(subg)
            model.train()
            # forward
            pred = model(subg)
            batch_labels = subg.ndata['label']

            if multilabel:
                loss = F.binary_cross_entropy_with_logits(pred, batch_labels, reduction='sum',
                                                          weight=subg.ndata['l_n'].unsqueeze(1))
            else:
                loss = F.cross_entropy(pred, batch_labels, reduction='none')
                loss = (subg.ndata['l_n'] * loss).sum()

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm(model.parameters(), 5)
            optimizer.step()
            iter_time = time.time()-iter_start
            dd.append(iter_time)

            if j  == len(loader)-1:
            #if j % interval == 0:
                eval_start = time.time()
                if cpu_flag and cuda:  # Only when we have shifted model to gpu and we need to shift it back on cpu
                    model = model.to('cpu')
                #val_f1_mic, val_f1_mac = evaluate(
                #    model, g, labels, val_mask, multilabel)
                model.eval()
                val_f1_mic, val_f1_mac = evaluate(
                    model, g, labels, test_mask, multilabel)
                print(
                    " F1-mic {:.4f},  F1-mac {:.4f}".format(val_f1_mic, val_f1_mac))
                #if val_f1_mic > best_f1:
                #    best_f1 = val_f1_mic
                #    print('new best val f1:', best_f1)
                #    torch.save(model.state_dict(), os.path.join(
                #        log_dir, 'best_model_{}.pkl'.format(task)))
                eval_time = time.time()-eval_start
                ev.append(eval_time)
                dur_ = (time.time()-start_time) -np.sum(ev)
                logline = f'{task},online_{args.online},ep{epoch}_it{j},{dur_:.4f},{val_f1_mic:.4f},{val_f1_mac:.4f}\n'
                with open(args.csv,'a') as f :
                    f.write(logline)
                if cpu_flag and cuda:
                    model.cuda()


            #if j == len(loader) - 1:
            #    model.eval()
            #    with torch.no_grad():
            #        train_f1_mic, train_f1_mac = calc_f1(batch_labels.cpu().numpy(),
            #                                             pred.cpu().numpy(), multilabel)
            #        print(f"epoch:{epoch + 1}/{args.n_epochs}, Iteration {j + 1}/"
            #              f"{len(loader)}:training loss", loss.item())
            #        print("Train F1-mic {:.4f}, Train F1-mac {:.4f}".format(train_f1_mic, train_f1_mac))
        dur.append(time.time()-ep_start)
        # evaluate
        #model.eval()
        #if epoch % args.val_every == 0:
        #    if cpu_flag and cuda:  # Only when we have shifted model to gpu and we need to shift it back on cpu
        #        model = model.to('cpu')
        #    #val_f1_mic, val_f1_mac = evaluate(
        #    #    model, g, labels, val_mask, multilabel)
        #    val_f1_mic, val_f1_mac = evaluate(
        #        model, g, labels, test_mask, multilabel)
        #    print(
        #        " F1-mic {:.4f},  F1-mac {:.4f}".format(val_f1_mic, val_f1_mac))
        #    if val_f1_mic > best_f1:
        #        best_f1 = val_f1_mic
        #        print('new best val f1:', best_f1)
        #        torch.save(model.state_dict(), os.path.join(
        #            log_dir, 'best_model_{}.pkl'.format(task)))
        #    logline = f'{task},online_{args.online},{epoch},{np.sum(dur):.4f},{val_f1_mic:.4f},{val_f1_mac:.4f}\n'
        #    with open(args.csv,'a') as f :
        #        f.write(logline)
        #    if cpu_flag and cuda:
        #        model.cuda()

    end_time = time.time()
    print(f'training using time {end_time - start_time}')

    # test
    if args.use_val:
        model.load_state_dict(torch.load(os.path.join(
            log_dir, 'best_model_{}.pkl'.format(task))))
    if cpu_flag and cuda:
        model = model.to('cpu')
    test_f1_mic, test_f1_mac = evaluate(
        model, g, labels, test_mask, multilabel)
    print("Test F1-mic {:.4f}, Test F1-mac {:.4f}".format(test_f1_mic, test_f1_mac))

if __name__ == '__main__':
    warnings.filterwarnings('ignore')

    parser = argparse.ArgumentParser(description='GraphSAINT')
    parser.add_argument("--task", type=str, default="ppi_n", help="type of tasks")
    parser.add_argument("--online", dest='online', action='store_true', help="sampling method in training phase")
    parser.add_argument("--gpu", type=int, default=0, help="the gpu index")
    parser.add_argument("--csv", type=str, default='test.csv')
    task = parser.parse_args().task
    args = argparse.Namespace(**CONFIG[task])
    args.online = parser.parse_args().online
    args.gpu = parser.parse_args().gpu
    args.csv = parser.parse_args().csv
    print(args)

    main(args, task=task)
