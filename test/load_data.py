import numpy as np
import pickle

matrix = "E:\\learning\\malware\\datas\\virushare-20\\data\\matrix.npy"
api ="E:\\learning\\malware\\datas\\virushare-20\\data\\train\\api.npy"
# 使用numpy.load()函数加载.npy文件
loaded_array = np.load(api, allow_pickle=True)

print(loaded_array)
# print(loaded_array[-1])

# print(pickle.loads(loaded_array['archive/data/3013123774928']))

# path = 'E:/learning/malware/datas/sp23/real_world/malware.npz'
# data = np.load(path)
# x_train, y_train, x_test, y_test = data['X_train'], data['y_train'], data['X_test'], data['y_test']
# print(x_train.shape)
# print(y_train.shape)
# print(x_test.shape)
# print(y_test.shape)
#
# from collections import Counter
# print(Counter(y_train))
#
# print(x_train[0])