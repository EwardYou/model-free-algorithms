source activate gym

export TF_CPP_MIN_LOG_LEVEL=3
export OPENBLAS_NUM_THREADS=1
# PYTHONPATH=$(dirname $(pwd)):$PYTHONPATH  xvfb-run -s "-screen 0 1400x900x24" -a python train.py
# train multiple agent
PYTHONPATH=$(dirname $(pwd)):$PYTHONPATH  xvfb-run -s "-screen 0 1400x900x24" -a python distributed_train.py
