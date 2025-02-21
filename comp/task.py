import torch as t
import numpy as np
from torch.nn.utils.rnn import pad_sequence, pack_padded_sequence
from torch.utils.data import DataLoader
import random as rd
import time

from comp.metric import Metric
from utils.magic import magicSeed, randPermutation, randList
from comp.sampler import EpisodeSampler, BatchSampler
from utils.profiling import costTimeWithFuncName, ClassProfiler
from comp.dataset import FusedDataset

#########################################
# 基于Episode训练的任务类，包含采样标签空间，
# 采样实验样本，使用dataloader批次化序列样
# 本并且将任务的标签标准化。
#
# 调用episode进行任务采样和数据构建。
# 在采样时会自动缓存labels，输入模型的输出
# 调用accuracy计算得到正确率
#########################################
class EpisodeTask:
    def __init__(self, k, qk, n, N, dataset: FusedDataset, cuda=True,
                 label_expand=False, parallel=None,
                 task_type=None, data_source=None):
        self.UseCuda = cuda
        self.Dataset = dataset
        self.Expand = label_expand
        self.Parallel = parallel
        self.DataSource = data_source

        if task_type != None:
            assert task_type in ['Train', 'Validate', 'Test']
        self.TaskType = task_type

        # n = k + qk
        assert k + qk <= N, '支持集和查询集采样总数大于了类中样本总数!'
        self.Params = {'k': k, 'qk': qk, 'n': n, 'N': N}

        self.SupSeqLenCache = None
        self.QueSeqLenCache = None

        self.LabelsCache = None
        self.Metric = Metric(n, expand=label_expand)

        self.TaskSeedCache = None
        self.SamplingSeedCache = None

    def _readParams(self):
        params = self.Params
        k, qk, n, N = params['k'], params['qk'], params['n'], params['N']

        return k, qk, n, N

    def _getLabelSpace(self, seed=None):
        seed = magicSeed() if seed is None else seed
        self.TaskSeedCache = seed

        rd.seed(seed)
        classes_list = [i for i in range(self.Dataset.TotalClassNum)]  # 87
        sampled_classes = rd.sample(classes_list, self.Params['n'])

        # print('label space: ', sampled_classes)
        return sampled_classes

    def _getTaskSampler(self, label_space, seed=None):
        sampling_seed = magicSeed() if seed is None else seed

        self.SamplingSeedCache = sampling_seed

        k, qk, n, N = self._readParams()

        seed_for_each_class = randList(num=len(label_space),  # 采样每个class内部的seed
                                       seed=sampling_seed,
                                       allow_duplicate=True)

        support_sampler = EpisodeSampler(k, qk, N, label_space, seed_for_each_class, mode='support')
        query_sampler = EpisodeSampler(k, qk, N, label_space, seed_for_each_class, mode='query')        # make query set labels cluster

        return support_sampler, query_sampler

    def _getEpisodeData(self, support_sampler, query_sampler):
        k, qk, n, N = self._readParams()
        # support_sampler.InstDict: {4: [12, 8, 1, 7, 9], ...*10}
        # support_loader = DataLoader(self.Dataset, batch_size=k * n,
        #                             sampler=support_sampler, collate_fn=batchSequence)#getBatchSequenceFunc())
        # query_loader = DataLoader(self.Dataset, batch_size=qk * n,
        #                           sampler=query_sampler, collate_fn=batchSequence)#getBatchSequenceFunc())
        #
        # support_seqs, support_imgs, support_lens, support_labels = support_loader.__iter__().next()
        # query_seqs, query_imgs, query_lens, query_labels = query_loader.__iter__().next()
        #

        support_seqs, support_imgs, support_lens, support_labels = self.Dataset.sample(support_sampler, batch_size=k*n)
        query_seqs, query_imgs, query_lens, query_labels = self.Dataset.sample(query_sampler, batch_size=qk*n)

        # 将序列长度信息存储便于unpack
        self.SupSeqLenCache = support_lens
        self.QueSeqLenCache = query_lens

        return (support_seqs, support_imgs, support_lens, support_labels), \
               (query_seqs, query_imgs, query_lens, query_labels)

    def _taskLabelNormalize(self, sup_labels, que_labels):
        k, qk, n, N = self._readParams()

        # 由于分类时是按照类下标与支持集进行分类的，因此先出现的就是第一类，每k个为一个类
        # size: [ql*n]
        sup_labels = sup_labels[::k].repeat(len(que_labels))  # 支持集重复q长度[10*50]次，代表每个查询都与所有支持集类比较
        que_labels = que_labels.view(-1, 1).repeat((1, n)).view(-1)  # 查询集重复n[50*10]次

        assert sup_labels.size(0) == que_labels.size(0), \
            '扩展后的支持集和查询集标签长度: (%d, %d) 不一致!' % (sup_labels.size(0), que_labels.size(0))

        # 如果进行扩展的话，每个查询样本的标签都会是n维的one-hot（用于MSE）
        # 不扩展是个1维的下标值（用于交叉熵）
        if not self.Expand:
            que_labels = t.argmax((sup_labels == que_labels).view(-1, n).int(), dim=1)
            return que_labels.long()
        else:
            que_labels = (que_labels == sup_labels).view(-1, n)
            return que_labels.float()

    def episode(self, task_seed=None, sampling_seed=None):
        raise NotImplementedError

    def labels(self):
        return self.LabelsCache

    def metrics(self, out, is_labels=False, metrics=['acc']):
        return self.Metric.stat(out, is_labels, metrics)


##########################################################
# 常规episode任务，通用于meta-learning模型中，episode返回中
# 包含了所有可能用到的数据
##########################################################
class RegularEpisodeTask(EpisodeTask):
    def __init__(self, k, qk, n, N, dataset, cuda=True,
                 label_expand=False, parallel=None,
                 task_type='Train'):
        super(RegularEpisodeTask, self).__init__(k, qk, n, N, dataset, cuda, label_expand, parallel, task_type)

    # @ClassProfiler("regular_episode")
    def episode(self, task_seed=None, sampling_seed=None):
        k, qk, n, N = self._readParams()  # 5, 5, 10, 20
        # 87类选n类
        label_space = self._getLabelSpace(task_seed)
        support_sampler, query_sampler = self._getTaskSampler(label_space, sampling_seed)
        # support_seqs: [n*k, seq_len:300]; support_imgs: [n*k, 1, 224, 224]
        (support_seqs, support_imgs, support_lens, support_labels), \
        (query_seqs, query_imgs, query_lens, query_labels) = self._getEpisodeData(support_sampler, query_sampler)

        query_labels = self._taskLabelNormalize(support_labels, query_labels) # TODO ????
        support_labels = self._taskLabelNormalize(support_labels, support_labels)
        self.LabelsCache = query_labels
        # 1.9修复bug：metric的labels必须在标签归一化之后更新
        self.Metric.updateLabels(query_labels)

        # 重整数据结构，便于模型读取任务参数 [50, ...] -> [10, 5, ...]
        if support_seqs is not None:
            support_seqs = support_seqs.view(n, k, -1)
            query_seqs = query_seqs.view(n * qk, -1)
        if support_imgs is not None:
            img_width, img_height = support_imgs.size()[-2:]
            support_imgs = support_imgs.view(n, k, 1, img_width, img_height)
            query_imgs = query_imgs.view(n*qk, 1, img_width, img_height)    # 注意，此处的qk指每个类中的查询样本个数，并非查询集长度

        return (support_seqs, support_imgs, support_lens, support_labels), \
               (query_seqs, query_imgs, query_lens, query_labels)


class BatchSampledTask(RegularEpisodeTask):
    def __init__(self, k, qk, n, N, dataset, total_epoch,
                 cuda=True, label_expand=False, parallel=None,
                 task_type='Train',
                 task_seq_seed=None, sampling_seq_seed=None):
        super(BatchSampledTask, self).__init__(k, qk, n, N, dataset, cuda, label_expand, parallel, task_type)
        self.TotalEpoch = total_epoch

        # 此处默认假定支持集和查询集没有shuffle，都是按照k/qkg个一个类的顺序返回的
        self.SupportLabels = t.LongTensor([i for i in range(n)])[:,None].repeat(1,k).view(-1,).cuda()
        self.QueryLabels = t.LongTensor([i for i in range(n)])[:,None].repeat(1,qk).view(-1,).cuda()


        support_batch_sampler, query_batch_sampler = self._makeBatchSampler(task_seq_seed, sampling_seq_seed)
        # 为数据集构建DataLoader
        self.Dataset.addBatchSampler(support_batch_sampler, query_batch_sampler)
        # 由于query_labels不再改变，因此直接在初始化时写一次写死
        self.Metric.updateLabels(self.QueryLabels)

    def _makeBatchSampler(self, task_seq_seed=None, sampling_seq_seed=None):
        k, qk, n, N = self._readParams()

        if task_seq_seed is None:
            task_seq_seed = magicSeed()
        if sampling_seq_seed is None:
            sampling_seq_seed = magicSeed()

        # 采样任务序列种子
        task_seq_seeds = randList(self.TotalEpoch, seed=task_seq_seed)
        # 采样采样序列种子
        sampling_seq_seeds = randList(self.TotalEpoch, seed=sampling_seq_seed)

        # 利用任务序列种子采样标签空间
        label_space_seq = []
        for seed in task_seq_seeds:
            label_space = self._getLabelSpace(seed)
            label_space_seq.append(label_space)

        sampled_support_indexes_seq = []
        sampled_query_indexes_seq = []
        for label_space, sampling_seed in zip(label_space_seq, sampling_seq_seeds):
            # 对于一个标签空间，采样每个class内部的seed
            class_wise_seeds = randList(num=len(label_space),
                                        seed=sampling_seed,
                                        allow_duplicate=True)
            support_episode_items = []
            query_episode_items = []
            for class_, class_seed in zip(label_space, class_wise_seeds):
                perm = randPermutation(N, class_seed)

                # 排列前k个是支持集的类内偏移
                support_items = [class_ * N + i for i in perm[:k]]
                # 排列第k到qk+k个是查询集的类内偏移
                query_items = [class_ * N + i for i in perm[k:k+qk]]

                support_episode_items.extend(support_items)
                query_episode_items.extend(query_items)

            sampled_support_indexes_seq.append(support_episode_items)
            sampled_query_indexes_seq.append(query_episode_items)

        support_batch_sampler = BatchSampler(sampled_support_indexes_seq)
        query_batch_sampler = BatchSampler(sampled_query_indexes_seq)

        return support_batch_sampler, query_batch_sampler

    # @ClassProfiler('batch_sample_episode')
    def episode(self, task_seed=None, sampling_seed=None):
        k, qk, n, N = self._readParams()

        support_seqs, support_imgs, support_lens, support_labels = self.Dataset.sampleByBatch('support')
        query_seqs, query_imgs, query_lens, query_labels = self.Dataset.sampleByBatch('query')

        # 重整数据结构，便于模型读取任务参数
        if support_seqs is not None:
            support_seqs = support_seqs.view(n, k, -1)
            query_seqs = query_seqs.view(n * qk, -1)
        if support_imgs is not None:
            img_width, img_height = support_imgs.size()[-2:]
            support_imgs = support_imgs.view(n, k, 1, img_width, img_height)
            query_imgs = query_imgs.view(n*qk, 1, img_width, img_height)    # 注意，此处的qk指每个类中的查询样本个数，并非查询集长度

        return (support_seqs, support_imgs, support_lens, self.SupportLabels), \
               (query_seqs, query_imgs, query_lens, self.QueryLabels)


########################################################
# dataloader从dataset中收集数据的收集方法
# 将逐个抽出的api，img，seqlen和label进行归类收集返回
# 返回的出口在dataloader中
########################################################
def batchSequence(data):
    seqs, imgs, lens, labels = [], [], [], []

    for seq, img, len_, label in data:
        seqs.append(seq.tolist())
        imgs.append(img.tolist())
        lens.append(len_)
        labels.append(label)


    return t.LongTensor(seqs), t.Tensor(imgs), lens, t.LongTensor(labels)

if __name__ == '__main__':
    t = RegularEpisodeTask()