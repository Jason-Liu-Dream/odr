from numpy import histogram

__author__ = 'caoym'


def removeBlack(src):
    #ȥ����ɫ����
    imhist, bins = histogram(src.flatten(),256,True)