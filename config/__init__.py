import platform
import os

from .structs import *
from .structs import _loadJsonConfig
from .const import *


def _setCudaDevice(task_config: TaskConfig):
    if task_config.DeviceId is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(task_config.DeviceId)


def _loadEnv():
    system_node = platform.node()
    env_cfg = _loadJsonConfig(file_name='env.json',
                              err_msg="没有合适的env config文件相对路径")
    return env_cfg['platform-node'][system_node]

env = EnvConfig(_loadEnv())
run_cfg = _loadJsonConfig(file_name="train.json",
                          err_msg="没有合适的run config文件相对路径")
task = TaskConfig(run_cfg)
train = TrainingConfig(run_cfg)
optimize = OptimizeConfig(run_cfg)
params = ParamsConfig(run_cfg)
plot = PlotConfig(run_cfg)

test_cfg = _loadJsonConfig(file_name="test.json",
                           err_msg="没有合适的test config文件相对路径")
test = TestConfig(test_cfg)

_setCudaDevice(task)

__all__ = ["env", "task", "train", "optimize", "params", "plot", "test"]


def printRunConfigSummary(task_config: TaskConfig=task, model_config: ParamsConfig=params):
    print("**************************************************")
    print(f"{model_config.ModelName} {task_config.Dataset} ver.{task_config.Version}")
    print(f"n:k:qk = {task_config.Episode.n}/{task_config.Episode.k}/{task_config.Episode.qk}")
    print(f"Cuda: {task_config.DeviceId}")
    print("**************************************************")


def reloadAllTestConfig(cfg_path):
    new_run_cfg = loadJson(cfg_path)

    # 测试时，只需要重加载模型参数和优化参数即可
    global optimize, params
    optimize = OptimizeConfig(new_run_cfg)
    params = ParamsConfig(new_run_cfg)

    _setCudaDevice(test.Task)
