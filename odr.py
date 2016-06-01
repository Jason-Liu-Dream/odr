# coding=utf-8
from time import time
from scipy import signal
from scipy.cluster.vq import whiten
from skimage.morphology import skeletonize
from sklearn import preprocessing
from sklearn.cluster import DBSCAN, MiniBatchKMeans, MeanShift
from sklearn.cross_validation import train_test_split
from sklearn.decomposition import PCA, RandomizedPCA
from sklearn.feature_extraction.text import TfidfTransformer
from sklearn.feature_selection import chi2
from sklearn.grid_search import GridSearchCV
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import classification_report
from sklearn.metrics import confusion_matrix
from sklearn.multiclass import OneVsRestClassifier
from sklearn.svm import SVC, LinearSVC
from peewee import *

__author__ = 'caoym'

import os
import cv2
import math
import numpy
from scipy import stats, ndimage
from scipy.ndimage import measurements,filters
from skimage.filters import threshold_adaptive
from utils import DB
from PIL import Image
from matplotlib.pyplot import *
from skimage.feature import daisy, hog
from sklearn.neighbors import KNeighborsClassifier


def train_svc(target_names,labs,data):
        print "start svm train..."
        X_train, X_test, y_train, y_test = train_test_split(data, labs, test_size=0.2, random_state=42)

        ###############################################################################
        # Train a SVM classification model

        print("Fitting the classifier to the training set")
        t0 = time()
        param_grid = {'C': [1e3, 5e3, 1e4, 5e4, 1e5],
                      'gamma': [0.0001, 0.0005, 0.001, 0.005, 0.01, 0.1], }

        gscv = GridSearchCV(SVC(kernel='rbf',probability=True,class_weight="auto"), param_grid=param_grid)
        #'linear', 'poly', 'rbf', 'sigmoid', 'precomputed'
        #clf = SVC(kernel='linear',probability=True,class_weight="auto")
        svc = gscv.fit(data, labs)
        print("done in %0.3fs" % (time() - t0))
        print("Best estimator found by grid search:")
        print(svc.best_estimator_)
        print "score :",svc.score(data, labs)
        ###############################################################################
        # Quantitative evaluation of the model quality on the test set

        print("Predicting on the test set")
        t0 = time()
        y_pred = svc.predict(X_test)

        print("done in %0.3fs" % (time() - t0))

        print(classification_report(y_test, y_pred, target_names=target_names))
        print(confusion_matrix(y_test, y_pred))
        return svc

class AutoSave(object):
    def __init__(self):
        self.__dict__['_attrs'] = {}

    def __setattr__(self, name, value):
        DB.db.connect()
        with DB.db.transaction():
            DB.TrainingResult.delete().where(DB.TrainingResult.name == self.__class__.__name__+"_"+name).execute()

            tr = DB.TrainingResult()
            tr.name = self.__class__.__name__+"_"+name
            tr.data = value
            tr.save()
            self._attrs[name] = value
            print tr.name + " saved"

    def __getattr__(self, item):
        if self.__dict__.has_key(item):
            return self.__dict__ [item]

        if self._attrs.has_key(item):
            return self._attrs[item]
        DB.db.connect()
        self._attrs[item] = DB.TrainingResult.select(DB.TrainingResult.data).where(DB.TrainingResult.name == self.__class__.__name__+"_"+item).get().data
        return self._attrs[item]

class WordCluster(AutoSave):

    def __init__(self):
        AutoSave.__init__(self)
    """
    通过聚类提取视觉词汇
    """
    def fit(self, samples_dir):
        self.make_features(samples_dir)
        self.create_descriptors()
        self.cluster_lv1()
        self.cluster_lv2()
        self.create_classifier()

    def predict(self,img_file):
        print 'start WordCluster::predict %d'%time()
        '''
        预测文本中的文字
        :param img_file:
        :return:
        '''
        img = Image.open(img_file).convert('L')
        img = numpy.array(img,'uint8')
        features = self.get_features_from_image(img)

        desc = []
        for i in features:
            desc.append(self.get_descriptor_lv1(i))
        #预测lv1
        lv1 = self._lv1.predict(desc)
        #预测lv2
        lv2 = []
        for i in range(0,len(lv1)):
            lab = lv1[i]
            lv2.append((lab,+self._lv2[lab].predict(self.get_descriptor_lv2(features[i]))))

        print 'in WordCluster::predict %d'%time()
        fitted = cv2.PCAProject(numpy.array(desc), self._pca_mean, self._pca_eigenvectors)
        print 'in WordCluster::predict %d'%time()
        #words = self._svm.predict_all(fitted)
        words = self._clf.predict(fitted)
        print 'end WordCluster::predict %d'%time()

        print 'end WordCluster::predict %d'%time()
        figure()
        gray()
        imshow(img)

        figure()
        gray()
        for i in range(0,min(400,len(features))):
            subplot(20,20,i+1)
            axis('off')
            imshow(features[i])

        figure()
        gray()
        for i in range(0,min(400,len(words))):
            subplot(20,20,i+1)
            axis('off')
            img = DB.Vocabulary\
                .select(DB.Vocabulary.feature).join(DB.Feature)\
                .where(DB.Vocabulary.word == words[i]).get().feature.img
            img = numpy.array(img)
            imshow(img)
        show()

        return words

    def get_words_count(self):
        return DB.Vocabulary.select(DB.Vocabulary.lv1,DB.Vocabulary.lv2).where((DB.Vocabulary.lv2 != -1) & (DB.Vocabulary.lv1 != -1)).distinct().count()

    def get_samples(self):
        '''
        获取所有样本
        :return: {(lab,filename):[11,222,333,], ...}

        '''
        docs = {}
        for f in DB.Vocabulary.select(DB.Vocabulary.lv1,DB.Vocabulary.lv2,DB.Feature.label,DB.Feature.docname).join(DB.Feature).where((DB.Vocabulary.lv2 != -1) & (DB.Vocabulary.lv1 != -1)).iterator():
            assert isinstance(f,DB.Vocabulary)
            key = (f.feature.label, f.feature.docname)
            if not docs.has_key(key):
                docs[key]=[]
            docs[key].append((f.lv1,f.lv2))
        return docs

    def create_classifier(self):
        DB.db.connect()
        clf = SGDClassifier( loss="modified_huber")
        labs_map = NameToIndex()

        with DB.db.transaction():
            offset = 0
            words_count = self.get_words_count()
            classes = numpy.arange(0,words_count)
            x_all = []
            y_all = []
            while True:
                print ' %d partial_fit %d'%(time(),offset)
                query = DB.Vocabulary\
                    .select(DB.Vocabulary.lv1, DB.Vocabulary.lv2)\
                    .join(DB.PcaModel, on=(DB.Vocabulary.feature == DB.PcaModel.feature)).order_by( DB.Vocabulary.feature).offset(offset).limit(1000)\
                    .tuples().iterator()
                features = numpy.array(map(lambda x:[x[0]]+list(x[1]),query))
                offset += len(features)
                if len(features) == 0:
                    break

                Y = features[:,0]
                X = features[:,1:]

                labs = []
                for lab in Y:
                    labs.append(labs_map.map(lab))

                if(len(x_all)<10000):
                    x_all = x_all + X.tolist()
                    y_all = y_all + labs
                labs = numpy.array(labs)

                #clf = LinearSVC()
                #clf = OneVsRestClassifier(SVC(probability=True, kernel='linear'))
                #clf.fit(X,labs)
                clf.partial_fit(X,labs,classes)
                print clf.score(x_all,y_all)

            DB.TrainingResult.delete().where(DB.TrainingResult.name == self.__class__.__name__+"_clf").execute()
            DB.TrainingResult.delete().where(DB.TrainingResult.name == self.__class__.__name__+"_labs_map").execute()

            tr = DB.TrainingResult()
            tr.name = self.__class__.__name__+"_clf"
            tr.data = clf
            tr.save()

            tr = DB.TrainingResult()
            tr.name = self.__class__.__name__+"_labs_map"
            tr.data = labs_map
            tr.save()


    def merge_words_for_labels(self):
        '''
        合并所有分类的词汇，并重新聚类
        '''
        print "start merge_words_for_labels ..."
        query = DB.PcaModel.select(DB.PcaModel.feature,DB.PcaModel.pca)\
            .join(DB.SubVocabulary,on =(DB.PcaModel.feature == DB.SubVocabulary.feature))\
            .where(DB.SubVocabulary.word != -1).tuples().iterator()

        features = numpy.array(map(lambda x:[x[0]]+list(x[1]),query))

        print "%d features"%(len(features))
        start = time()
        print "start time %s "%(start)

        index = features[:,0]
        data = features[:,1:]

        prepro = preprocessing.Normalizer()
        data = prepro.fit_transform(data)
        features = None

        '''bow = cv2.BOWKMeansTrainer(50000)
        data = numpy.array(data,"float32")
        center = bow.cluster(data);'''
        cluster = DBSCAN(0.52,algorithm='ball_tree', min_samples=2,leaf_size=3000)
        #cluster = Birch(threshold=0.028, n_clusters=None,copy=False)
        #cluster = MeanShift();
        #
        #cluster = MiniBatchKMeans(init='k-means++', n_clusters=50000,random_state=0,batch_size=50000,reassignment_ratio=0,verbose=1,max_iter=100)
        res = cluster.fit(data)

        #cluster = KMeans(n_clusters=50000);
        print "cost time %s"%(time()-start)

        types = {}
        for i in range(0,len(res.labels_)):
            type = res.labels_[i]
            if not types.has_key(type):
                types[type] = []
            types[type].append(i)

        #print "%d words, %d core samples, %d noise"%(len(types.keys()),len(res.core_sample_indices_), len(types[-1]) )
        print "%d words"%len(types.keys())
        types = sorted(types.iteritems(),key=lambda i:len(i[1]),reverse=True)

        '''figure()
        line = 0
        for k,v in types:
            if k ==-1:
                continue
            print k,v;
            for i in range(0,min(20,len(v))):
                subplot(20,20,line*20+i+1)
                axis('off')
                f = DB.Feature(DB.Feature.img).get(DB.Feature.id == index[v[i]])
                imshow(f.img)
            line += 1
            if line == 20:
                line = 0
                show()
        show()'''

        '''for k,v in types:
            #if k ==-1:
            #    continue
            print k,v;
            for i in range(0,min(400,len(v))):
                subplot(20,20,i+1)
                axis('off')
                f = DB.Feature(DB.Feature.img).get(DB.Feature.id == index[v[i]])
                imshow(f.img)
            show()'''
        DB.db.connect()
        with DB.db.transaction():
            DB.Vocabulary.drop_table(fail_silently=True)
            DB.Vocabulary.create_table()
            DB.Words.drop_table(fail_silently=True)
            DB.Words.create_table()
            for k,v in types:
                if k == -1:
                    continue
                word = DB.Words()
                word.chi = 0
                word.idf = 0
                word.ignore = False
                word.save(force_insert=True)
                for w in v:
                    DB.Vocabulary.insert(word = word, feature = index[w] ).execute()

        print "done merge_words_for_labels"
        return cluster

    def display_words(self):
        DB.db.connect()
        #select word_id,count(*)as count from vocabulary join feature on vocabulary.feature_id = feature.id group by word_id order by count DESC
        words = DB.Vocabulary.select(DB.Vocabulary.lv1,DB.Vocabulary.lv2,fn.COUNT().alias('count'))\
            .join(DB.Feature)\
            .where(DB.Vocabulary.lv2 != -1)\
            .group_by(DB.Vocabulary.lv1,DB.Vocabulary.lv2)\
            .order_by(SQL('count desc'))\
            .tuples().iterator()

        figure()
        for i in words:
            print i;
            features = DB.Feature.select().join(DB.Vocabulary).where((DB.Vocabulary.lv1 == i[0]) & (DB.Vocabulary.lv2 == i[1])).limit(400).iterator()
            pos = 0
            for i in features:
                pos += 1
                subplot(20,20,pos)
                axis('off')
                imshow(i.img)
            show()

        '''for k,v in types:
            #if k ==-1:
            #    continue
            print k,v;
            for i in range(0,min(400,len(v))):
                subplot(20,20,i+1)
                axis('off')
                f = DB.Feature(DB.Feature.img).get(DB.Feature.id == index[v[i]])
                imshow(f.img)
            show()'''

        pass

    def cluster_lv1(self):
        print "start cluster_lv1 ..."
        offset = 0
        limit = 3000
        cluster = MiniBatchKMeans(n_clusters=100,verbose=1,max_no_improvement=None,reassignment_ratio=1.0)
        while True:
            print ' %d partial_fit %d'%(time(),offset)

            query = DB.DescriptorModel.select(DB.DescriptorModel.feature,DB.DescriptorModel.lv1).offset(offset).limit(limit).tuples().iterator()
            features = numpy.array(map(lambda x:[x[0]]+list(x[1]),query))

            if len(features) == 0:
                break
            offset += len(features)
            X = features[:,1:]
            cluster.partial_fit(X)

        DB.db.connect()
        with DB.db.transaction():
            DB.Vocabulary.drop_table(fail_silently=True)
            DB.Vocabulary.create_table()

            offset=0
            while True:
                print ' %d predict %d'%(time(),offset)
                query = DB.DescriptorModel.select(DB.DescriptorModel.feature,DB.DescriptorModel.lv1).offset(offset).limit(1000).tuples().iterator()
                features = numpy.array(map(lambda x:[x[0]]+list(x[1]),query))
                if len(features) == 0:
                    break
                offset += len(features)
                X = features[:,1:]
                Y = features[:,0]
                res = cluster.predict(X)

                for i in range(0,len(res)):
                    DB.Vocabulary.insert(lv1 = res[i],lv2=0, feature = Y[i]).execute()

        #print "%d words, %d core samples, %d noise"%(len(types.keys()),len(res.core_sample_indices_), len(types[-1]) )
        self._lv1 = cluster;
        print "done cluster_lv1"
        return cluster

    def cluster_lv2(self):
        print "start cluster_lv2 ..."
        DB.db.connect()
        clusters = {}
        word_count = 0
        with DB.db.transaction():
            maxid = DB.Vocabulary.select(fn.MAX(DB.Vocabulary.lv1).alias('max')).get().max
            for i in range(0,maxid+1):
                count = DB.Vocabulary.select(fn.COUNT().alias('count')).where(DB.Vocabulary.lv1 == i).get().count
                print "begin cluster_lv2 %d, %d"%(i,count)
                cluster = DBSCAN(6, min_samples=3)
                #cluster = MeanShift(bandwidth=0.79, cluster_all=False, min_bin_freq=3)

                query = DB.DescriptorModel.\
                    select(DB.DescriptorModel.feature, DB.DescriptorModel.lv2).\
                    join(DB.Vocabulary,on=(DB.Vocabulary.feature == DB.DescriptorModel.feature)).\
                    where(DB.Vocabulary.lv1 == i).\
                    tuples().iterator()

                features = numpy.array(map(lambda x:[x[0]]+x[1].flatten().tolist(),query))
                if len(features) == 0:
                    continue
                X = features[:,1:]
                Y = features[:,0]

                norm = preprocessing.Normalizer()
                X = norm.fit_transform(X)
                pca = RandomizedPCA(n_components=70, whiten=True)
                X = pca.fit_transform(X)
                cluster.fit(X)

                trainX = X[cluster.labels_ != -1]
                trainY = cluster.labels_[cluster.labels_ != -1]

                svc = train_svc(None,trainY,trainX)

                types = {}
                for i in range(0,len(cluster.labels_)):
                    type = cluster.labels_[i]
                    if not types.has_key(type):
                        types[type] = []
                    types[type].append(i)
                print "end cluster_lv2 %d words, %d core samples, %d noise"%(len(types.keys()),len(cluster.core_sample_indices_),  len(types[-1]) if types.has_key(-1) else 0 )
                #print "end cluster_lv2 %d words, %d core centers, %d noise"%(len(types.keys()),len(cluster.cluster_centers_),  len(types[-1]) if types.has_key(-1) else 0 )

                word_count += len(types.keys())
                '''figure()
                line = 0
                for k,v in types.iteritems():
                    if len(v)<2:
                        continue
                    if k ==-1:
                        continue
                    print k,v;
                    for i in range(0,min(20,len(v))):
                        subplot(20,20,line*20+i+1)
                        axis('off')
                        f = DB.Feature(DB.Feature.img).get(DB.Feature.id == Y[v[i]])
                        imshow(f.img)
                    line += 1
                    if line == 20:
                        line = 0
                        show()
                show()'''

                for id in range(0,len(cluster.labels_)):
                    type = cluster.labels_[id]
                    DB.Vocabulary.update(lv2 = type ).where(DB.Vocabulary.feature == Y[id]).execute()
                clusters[i] = [norm,pca,svc]


        self._lv2 = clusters
        print "done cluster_lv2"

    def cluster_words_all(self):
        '''
        对所有样本进行聚类
        '''

        print "start cluster_words_all ..."
        offset = 0
        limit = 300
        cluster = MiniBatchKMeans(n_clusters=100,verbose=1)
        while True:
            print ' %d partial_fit %d'%(time(),offset)

            query = DB.PcaModel.select(DB.PcaModel.feature,DB.PcaModel.pca)\
                .offset(offset).limit(limit).tuples().iterator()

            features = numpy.array(map(lambda x:[x[0]]+list(x[1]),query))
            if len(features) == 0:
                break
            offset += len(features)
            X = features[:,1:]
            cluster.partial_fit(X)

        DB.db.connect()
        with DB.db.transaction():
            DB.Vocabulary.drop_table(fail_silently=True)
            DB.Vocabulary.create_table()
            DB.Words.drop_table(fail_silently=True)
            DB.Words.create_table()

            offset=0
            while True:
                query = DB.PcaModel.select(DB.PcaModel.feature,DB.PcaModel.pca).offset(offset).limit(1000).tuples().iterator()
                features = numpy.array(map(lambda x:[x[0]]+list(x[1]),query))
                if len(features) == 0:
                    break
                offset += len(features)
                X = features[:,1:]
                Y = features[:,0]
                res = cluster.predict(X)

                for i in range(0,len(res)):

                    DB.Words.insert(id = res[i]).upsert().execute()
                    DB.Vocabulary.insert(word = res[i], feature = Y[i]).execute()

                DB.TrainingResult.delete().where(DB.TrainingResult.name == self.__class__.__name__+"_clf").execute()

                tr = DB.TrainingResult()
                tr.name = self.__class__.__name__+"_clf"
                tr.data = cluster
                tr.save()

        #print "%d words, %d core samples, %d noise"%(len(types.keys()),len(res.core_sample_indices_), len(types[-1]) )

        print "done cluster_words_all"
        #self.display_words()
        return cluster

    def cluster_words_for_labels(self):
        '''
        对每类文本进行聚类，提取词汇
        :return:
        '''
        DB.db.connect()
        with DB.db.transaction():
            DB.SubWords.drop_table(fail_silently=True)
            DB.SubVocabulary.drop_table(fail_silently=True)
            DB.SubVocabulary.create_table()
            DB.SubWords.create_table()
            '''query = DB.Feature.select(DB.Feature.id,DB.Feature.ori).distinct().iterator()
            i = 0
            for f in query:
                f.entropy = stats.entropy(numpy.array(f.ori).flatten())
                f.save()
                i += 1
                if i == 1000:
                    break
            return'''
            query = DB.Feature.select(
                DB.Feature.label
            ).distinct().tuples().iterator()

            for label in query:
                self.cluster_words_for_label(label[0])

    def cluster_words_for_label(self,label):
        '''
        每个分类独立计算bow
        '''
        print "start cluster_words_for_label %s ..."%label

        query = DB.PcaModel.select(DB.PcaModel.feature,DB.PcaModel.pca)\
            .join(DB.Feature)\
            .where((DB.Feature.ignore == 0) & (DB.Feature.label == label)).tuples().iterator()

        features = numpy.array(map(lambda x:[x[0]]+list(x[1]),query))
        start = time()
        print("cluster_words_for_label %s start time %s,%d features"%(label,start,len(features)))

        index = features[:,0]
        data = features[:,1:]

        prepro = preprocessing.Normalizer()
        data = prepro.fit_transform(data)
        features = None

        cluster = DBSCAN(0.55, algorithm='kd_tree', min_samples=3,leaf_size=300)
        res = cluster.fit(data)

        print "cluster cost time %s"%(time()-start)

        types = {}
        for i in range(0,len(res.labels_)):
            type = res.labels_[i]
            if not types.has_key(type):
                types[type] = []
            types[type].append(i)

        print "%d words, %d core samples, %d noise"%(len(types.keys()),len(res.core_sample_indices_), len(types[-1]) if types.has_key(-1) else 0 )
        types = sorted(types.iteritems(),key=lambda i:len(i[1]),reverse=True)

        '''figure()
        line = 0
        for k,v in types:
            if k ==-1:
                continue
            print k,v;
            for i in range(0,min(20,len(v))):
                subplot(20,20,line*20+i+1)
                axis('off')
                f = DB.Feature(DB.Feature.img).get(DB.Feature.id == index[v[i]])
                imshow(f.img)
            line += 1
            if line == 20:
                line = 0
                show()
        show()'''
        '''
        for k,v in types:
            #if k ==-1:
            #    continue
            print k,v;
            for i in range(0,min(400,len(v))):
                subplot(20,20,i+1)
                axis('off')
                f = DB.Feature(DB.Feature.img).get(DB.Feature.id == index[v[i]])
                imshow(f.img)
            show()'''

        for k,v in types:
            if k ==-1:
                continue
            words = DB.SubWords()
            words.ignore=False
            words.label=label
            words.save()
            for w in v:
                DB.SubVocabulary.insert(word=words,feature=index[w]).execute()

        print "done cluster_words_for_label %s"%label

    def create_descriptors_pca(self, dim = 90):
        '''
        计算描述子pca
        :param dim:
        :return:
        '''
        print("start create_descriptors_pca ...")
        query = DB.DescriptorModel.select(DB.DescriptorModel.id,DB.DescriptorModel.descriptor).tuples().iterator()
        features = numpy.array(map(lambda x:[x[0]]+list(x[1]),query))
        print("create_descriptors_pca,count=%d,dim=%d"%(len(features),dim))
        start = time()
        print("build eigenvectors start time %s"%start)

        mean, eigenvectors = cv2.PCACompute(features[:,1:],None,maxComponents=dim)
        fitted = cv2.PCAProject(features[:,1:],mean, eigenvectors)
        #pca = PCA(n_components=dim)
        #fitted = pca.fit_transform(features[:,1:])
        print("build eigenvectors cost time %s"%(time()-start))
        print("saving data ...")

        #scaler = preprocessing.MinMaxScaler()
        #pca = scaler.fit_transform(pca)
        DB.db.connect()
        with DB.db.transaction():
            DB.PcaModel.drop_table(fail_silently=True)
            DB.PcaModel.create_table()

            #res = DB.TrainingResult()
            #res.name = "daisy_pca"
            #res.data = pca
            #res.save()

            for i in range(0,len(fitted)):
                model = DB.PcaModel()
                model.pca = fitted[i]
                model.feature = features[i][0]
                model.save()

            DB.TrainingResult.delete().where(DB.TrainingResult.name =="pca_mean").execute()
            DB.TrainingResult.delete().where(DB.TrainingResult.name =="pca_eigenvectors").execute()
            tr = DB.TrainingResult()
            tr.name = "pca_mean"
            tr.data = mean
            tr.save()

            tr = DB.TrainingResult()
            tr.name = "pca_eigenvectors"
            tr.data = eigenvectors
            tr.save()

        print("create_descriptors_pca done")
    def get_descriptor_lvX(self,img):
        ori = img
        #img = cv2.bitwise_not(numpy.array(img))
        #img = threshold_adaptive(numpy.array(img), 40)
        #img = cv2.bitwise_not(img*255.)
        img = skeletonize(numpy.array(img)/255.)*255.
        '''figure()
        gray()
        subplot(221)
        imshow(ori)
        subplot(222)
        imshow(img)
        show()'''
        #e = stats.entropy(img.flatten())
        #if math.isnan(e) or math.isinf(e):
        #    return 0
        #else:
        #    return e
        descs = hog(numpy.array(img), orientations=4, pixels_per_cell=(10, 10),cells_per_block=(3, 3),visualise=False)
        '''figure()
        gray()
        imshow(img)
        figure()
        imshow(hpgimg)
        show()'''
        return descs

    def get_descriptor_lv2(self,img):

        img = cv2.Canny(numpy.uint8(img), 50, 200)
        #img = numpy.array(img.resize((48,16),Image.BILINEAR) )
        #img = cv2.bitwise_not(numpy.array(img))
        #img = threshold_adaptive(numpy.array(img), 40)
        descs = hog(img, orientations=6, pixels_per_cell=(4, 4),cells_per_block=(2, 2),visualise=False)
        #descs = hog(img, orientations=6, pixels_per_cell=(4, 4),cells_per_block=(3, 3),visualise=False)
        #descs2 = hog(img, orientations=9, pixels_per_cell=(16, 16),cells_per_block=(3, 3))
            #descs3 = hog(f.ori, orientations=9, pixels_per_cell=(32, 32),cells_per_block=(1, 2))
        #descs = descs2.flatten().tolist() + descs.flatten().tolist()
        '''figure()
        gray()
        imshow(img)
        figure()
        imshow(hpgimg)
        show()'''
        return descs

    def get_descriptor_lv1(self,img):
        #取每一列的最高点和最低点组合成特征描述符
        img = skeletonize(numpy.array(img)/255.)


        top=[] #上边曲线
        bottom = [] #下边曲线
        jumps = [] #每一列的跳跃次数
        h,w = img.shape
        for i in range(0,w):
            y = numpy.where(img[:,i]!=0)[0]
            if len(y) == 0:
                top.append(h)
                bottom.append(h)
                jumps.append(0)
            else:
                top.append(y.min()+1)
                bottom.append(h-y.max()-1)
                jump = 0
                last = 0
                for i in y:
                    if last != i:
                        jump += 1
                    last = i+1
                if last != h:
                    jump +=1
                jumps.append(jump)

        top = numpy.array(top,'float')/h
        bottom = numpy.array(bottom,'float')/h
        th = (h-img.sum(axis=0)).astype('float')/h
        jumps = numpy.array(jumps,'float')*2./h

        top = filters.gaussian_filter(top,2)
        bottom = filters.gaussian_filter(bottom,2)
        th = filters.gaussian_filter(th,2)
        jumps = filters.gaussian_filter(jumps,2)

        '''figure()
        gray()
        imshow(img)
        figure()
        plot(top)
        figure()
        plot(bottom)
        figure()
        plot(th)
        figure()
        plot(jumps)
        show()'''
        return th.tolist()+jumps.tolist()


    def get_descriptor2(self,img):
        img = img.convert('L')
        descs = hog(img, orientations=9, pixels_per_cell=(8, 8),cells_per_block=(3, 3))
        #descs2 = hog(img, orientations=9, pixels_per_cell=(16, 16),cells_per_block=(3, 3))
        #descs3 = hog(f.ori, orientations=9, pixels_per_cell=(32, 32),cells_per_block=(1, 2))
        #descs = descs2.flatten().tolist() + descs.flatten().tolist()
        #descs = daisy(f.ori,step=6,rings=2,histograms=8, visualize=False)
        #brief = BRIEF(patch_size=32)
        #brief.extract(numpy.array(i[0]), numpy.array([[16,16],[16,32],[16,48]]))
        '''figure()
        gray()
        axis('off')
        subplot(221)
        imshow(f.ori)
        subplot(222)
        imshow(img3)
        subplot(224)
        imshow(img)
        subplot(223)
        imshow(img2)
        show()'''
        return descs

    def create_descriptors(self):
        '''
        生成特征描述子
        :return:
        '''
        print("start create_descriptors")
        start = time()
        dbs = {}
        count = 0
        DB.db.connect()
        with DB.db.transaction():
            DB.DescriptorModel.drop_table(fail_silently=True)
            DB.DescriptorModel.create_table()
            lv1 = []
            for f in DB.Feature.select(DB.Feature.id,DB.Feature.ori).iterator():
                assert isinstance(f, DB.Feature)

                model = DB.DescriptorModel()
                model.lv1 = self.get_descriptor_lv1(f.ori)
                model.lv2 = self.get_descriptor_lv2(f.ori)
                model.feature = f.id
                model.save()
                count += 1
                if count %100 == 0:
                    print "did %d features"%count

        print "did %d features"%count
        print("create_descriptors done")

    def make_features(self, samples_dir):
        '''
        从图片中提取特征
        :param samples_dir:
        :return:
        '''
        print("WordCluster::make_features %s"%(samples_dir))
        DB.db.connect()
        with DB.db.transaction():
            DB.Feature.drop_table(fail_silently=True)
            DB.Feature.create_table()
            DB.TrainingResult.create_table(fail_silently=True)
            from_dir = os.listdir(samples_dir)
            features = []
            count = 0

            for type in from_dir:
                print("get_features_from_image for type %s..."%type)
                type_dir = "%s/%s"%(samples_dir,type)
                if not os.path.isdir(type_dir):
                    continue
                files = os.listdir(type_dir)

                for f in files:
                    from_file = type_dir+"/"+f
                    print("processing %s..."%from_file)
                    img = Image.open(from_file).convert('L')
                    img = numpy.array(img,'uint8')
                    res = self.get_features_from_image(img)
                    print("%s features found"%len(res))

                    for i in res:
                        count += 1
                        data = numpy.array(i).tolist()
                        mode = DB.Feature()
                        mode.ori = data
                        mode.img = i
                        mode.label = type
                        mode.docname = f
                        mode.entropy = stats.entropy(numpy.array(data).flatten())
                        mode.save()
            print("WordCluster::make_features done. %s features found"%count)

    def get_lines_from_image(self, src):
        '''
        从图片中拟合直线
        :param src:
        :return:
        '''
        #srcArr = numpy.array(src, 'uint8')
        # srcArr,T = rof.denoise(srcArr, srcArr)
        dst = cv2.Canny(src, 50, 200)
        lines = cv2.HoughLinesP(dst, 2, math.pi/180.0, 40, numpy.array([]), 50, 10)
        if lines is None:
            return None
        res = []
        for line in lines:
            x = (line[0][2] - line[0][0])
            y = (line[0][3] - line[0][1])
            xy = (x ** 2 + y ** 2) ** 0.5
            if 0 == xy:
                continue
            sin = y / xy
            angle = numpy.arcsin(sin) * 360 / 2 / numpy.pi

            res += [[line[0][0], line[0][1], line[0][2], line[0][3], 1, sin, angle]]
        return numpy.array(res)

    def adjust_slope(self, src):
        """
        矫正图片角度
        :type src: numpy.array
        """
        h, w = src.shape[:2]
        lines = self.get_lines_from_image(src)

        if lines is None:
            return src,0
        # 画出检查到的线
        #figure()
        #gray()
        #imshow(src)
        #for line in lines:
        #    plot([line[0], line[2]],[line[1], line[3]],'r-')
        #bins = len(lines)/5
        #n, bins = numpy.histogram(lines[:,6], bins=180, normed=True)
        #hSlope = bins[n.argmax(axis=0)]
        hSlope = numpy.median(lines[:,6])
        if abs(hSlope)<3:
            hSlope = 0
            dest = src
        else:
            dest = ndimage.rotate(src, hSlope)
        return dest,hSlope

    def adjust_size(self, src):
        """
        矫正图片大小
        通过一张图片中拟合到的直线长度与图片长宽的比例，确定图片的大小是否合适
        :type src: numpy.array
        """
        for loop in range(0,8):
            h, w = src.shape[:2]
            lines = self.get_lines_from_image(src)
            if lines is None:
                return src
            #左右最大间距
            left = lines[:,0]
            left = sorted(left, reverse=False)
            cut = len(left)/3+1
            left = numpy.median(numpy.array(left)[:cut])
            right = lines[:,2]
            right = sorted(right, reverse=True)
            cut = len(right)/3+1
            right = numpy.median(numpy.array(right)[:cut])
            maxlen = right - left
            #平均宽度
            arvlen = []
            for line in lines:
                arvlen += [line[2] - line[0]]

            arvlen = numpy.median(arvlen)
            if maxlen>arvlen*8:
                pil_im = Image.fromarray(numpy.uint8(src))
                src = numpy.array(pil_im.resize((int(w*0.8),int(h*0.8)) ,Image.BILINEAR))
            else:
                break
        return src

    def get_text_lines_from_image(self, src):
        '''
        将图片按文本单行切割
        :param src:
        :return:图片数组
        '''
        #调整大小
        src = self.adjust_size(src)

        temp = src.copy()
        #调整水平
        src = cv2.Canny(src, 100, 200)
        src,slope = self.adjust_slope(src)

        #src = cv2.erode(src,cv2.getStructuringElement(cv2.MORPH_CROSS,(1, 3)) )
        #src = cv2.dilate(src,cv2.getStructuringElement(cv2.MORPH_CROSS,(1, 3)) )

        src = cv2.dilate(src,cv2.getStructuringElement(cv2.MORPH_RECT,(40, 3)) )
        src = cv2.erode(src,cv2.getStructuringElement(cv2.MORPH_RECT,(40, 3)) )

        src = cv2.erode(src,cv2.getStructuringElement(cv2.MORPH_RECT,(5, 5)) )
        src = cv2.dilate(src,cv2.getStructuringElement(cv2.MORPH_RECT,(6, 5)) )

        src = 1*(src>128)

        labels_open, nbr_objects_open = measurements.label(src)

        #调整水平
        block_size = 40
        temp = threshold_adaptive(temp, block_size, offset=10)
        temp = numpy.array(temp,'uint8')*255
        temp = cv2.bitwise_not(temp)

        if slope != 0:
            temp = ndimage.rotate(temp, slope)
            #旋转后像素会平滑，重新二值化
            temp = cv2.bitwise_not(temp)
            temp = threshold_adaptive(temp, block_size,offset=20)
            temp = numpy.array(temp,'uint8')*255
            temp = cv2.bitwise_not(temp)

        lines = [];

        image  = numpy.zeros(numpy.array(src).shape)
        count = 0
        for i in range(1,nbr_objects_open+1):

            test = temp.copy()
            test[labels_open != i]=0
            box = self.bounding_box(test)
            x,y,w,h = box
            if h<10 or w<3:
                continue;
            #忽略靠近上下边的区域
            '''if y<2:
                continue
            if y+h> len(temp)-2:
                continue'''
            data = test[y:y+h, x:x+w]
            lines.append(data)

            copy = src.copy()*255.
            copy[labels_open != i]=0
            box = self.bounding_box(copy)
            x,y,w,h = box

            toerode = w/3
            if toerode <=1:
                continue

            copy = cv2.erode(copy,cv2.getStructuringElement(cv2.MORPH_RECT,(toerode, 1)) )
            copy = cv2.dilate(copy,cv2.getStructuringElement(cv2.MORPH_RECT,(toerode, 1)) )
            copy = 1*(copy>128)

            sub_labels_open, sub_nbr_objects_open = measurements.label(copy)
            if(sub_nbr_objects_open >1):
                for i in range(1,sub_nbr_objects_open+1):
                    test = temp.copy()
                    test[sub_labels_open != i]=0
                    box = self.bounding_box(test)
                    #count+=1
                    #image[sub_labels_open == i] = count
                    x,y,w,h = box
                    if h<10 or w<3:
                        continue;
                    #忽略靠近上下边的区域
                    if y<2:
                        continue
                    if y+h> len(temp)-2:
                        continue

                    data = test[y:y+h, x:x+w]
                    lines.append(data)

        '''figure()
        subplot(221)
        imshow(temp)
        subplot(222)
        imshow(image)
        subplot(223)
        imshow(labels_open)
        show()'''
        return lines

    def get_separaters_from_image(self, src,orisum):
        '''
        从单行文本图片中获取每个字之间的切割位置
        :param src:
        :param orisum:
        :return:
        '''

        src = signal.detrend(src)
        #找出拐点
        trend = False #False 下降，True上涨
        preval = 0 #上一个值
        points = [] #拐点
        pos = 0
        for i in src:
            if preval != i:
                if trend != (i>preval):
                    trend = (i>preval)
                    points += [[pos if pos == 0 else pos-1,preval,orisum[pos]]]
            pos = pos+1
            preval = i


        if len(points) <4:
            return [[0,len(src)]]
        points += [[pos,0]]

        #分隔
        i = 1
        points = numpy.array(points)
        count = points.shape[:2][0]
        left = 0
        res = []
        while i<count-1:
            if points[i][1] > points[i-1][1] and points[i][1] > points[i+1][1]:
                rightY = points[i+1][1]
                right = points[i+1][0]
                for x in range(i+1,count):
                    if rightY>=points[x][1]:
                        rightY=points[x][1]
                        right=points[x][0]
                    else:
                        break
                res += [[left,right]]
                left = right
            i += 1
        if left<src.shape[:2][0]:
            res += [[left,src.shape[:2][0]]]

        #搜索左右像素，调整分割点
        '''adjusted = []
        for i in res:
            l,r = i;
            adjusted.append([findMinPos(orisum,l,2),findMinPos(orisum,r,2)])'''
        return res

    def separate_words_from_image(self, src):
        '''
        从单行文本图片中切割出每一个字
        :param src:
        :return:
        '''
        '''figure()
        subplot(211)
        imshow(src)
        subplot(212)
        imshow(tmp)
        show()'''
        orisum = src.sum(axis=0)/255.0
        sum = filters.gaussian_filter(orisum,8)
        sep = self.get_separaters_from_image(sum,filters.gaussian_filter(orisum,2))

        words = []
        pos = 0
        h = len(src)
        for i in sep:
            #words += [[i[0],0,i[1]-i[0],h]]
            word = src[:,i[0]:i[1]]
            x,y,w,h = self.bounding_box(word)
            words.append([i[0]+x,y,w,h])
            #if word is not None:
           #     words += [word]
        return words

    def get_features_from_image(self, src, maxW=4.):
        '''
        :param src:
        :param maxW: 截取特征的最大宽度（以高度的倍数为单位，3倍差不多是3个字的宽度）
        :return:
        '''
        res = []

        lines = self.get_text_lines_from_image(src)
        for lineData in lines:
            words = numpy.array(self.separate_words_from_image(lineData))
            averageH = numpy.average(words[:,3]);

            for i in range(0,len(words)):
                startX,startY,startW,startH = words[i]
                passed = 0
                pos = i
                clipTp = startY
                clipBt = startY+startH
                while (len == 0 or passed+startW<averageH*maxW) and pos<len(words):
                    x,y,w,h = words[pos]
                    clipLt = startX
                    clipRt = x+w
                    clipBt = max(y+h,clipBt)
                    clipTp = min(y,clipTp)

                    data = lineData[clipTp:clipBt,clipLt:clipRt]
                    #
                    #data2 = Image.fromarray(data).resize((64,32),Image.BILINEAR)

                    #data = skeletonize(data/255)*255
                    #data = zhangSuen(data/255)*255
                    #删除连续的空白列
                    data = self.erase_black(data)
                    data = Image.fromarray(data).resize((96,32),Image.BILINEAR)
                    #data = numpy.array(data)
                    #data = skeletonize(data/255)*255

                    '''data = cv2.bitwise_not(numpy.array(data))
                    data = threshold_adaptive(data, 40, offset=10)
                    data = numpy.array(data,'uint8')*255
                    data = cv2.bitwise_not(data)
                    #data, distance = medial_axis(data, return_distance=True)
                    data = skeletonize(data/255)*255
                    data = Image.fromarray(data)'''
                    #figure()
                    #gray()
                    #imshow(data2)
                    #figure()
                    #imshow(labels_open)
                    #show()
                    #objs = measurements.find_objects(labels_open,nbr_objects_open)

                    '''figure()
                    gray()
                    imshow(data)
                    show()'''

                    #data = numpy.array(data,'uint8').tolist()
                    #data = scaler.fit_transform(data)
                    res.append(data);

                    passed += w
                    pos += 1
        '''figure()
        gray()
        for i in range(0,min(200,len(res))):
                if i>=20:
                    break
                subplot(20,20,i+1)
                axis('off')
                img = res[i]
                img = numpy.array(img)
                imshow(img)
        show()'''
        return res;

    def erase_black(self, src):
        '''
        剪切掉图片的纯黑色边框
        :param src:
        :return:
        '''
        i = 0
        while True :
            h,w = src.shape
            if i >=w:
                break;

            if(numpy.max(src[:,i:i+1])<128):
                if i == w-1 or numpy.max(src[:,i+1:i+2])<128:
                    src = numpy.delete(src,i,axis=1)
                    continue
            i += 1
        return src

    def bounding_box(self, src):
        '''
        矩阵中非零元素的边框
        :param src:
        :return:
        '''
        B = numpy.argwhere(src)
        if B.size == 0:
            return [0,0,0,0]
        (ystart, xstart), (ystop, xstop) = B.min(0), B.max(0) + 1

        return [xstart,ystart,xstop-xstart,ystop-ystart]

    def visualization_features(self, src,maxW=4.):
        figure()
        gray()
        features = self.get_features_from_image(src,maxW)

        i = 0
        for img in features:
            subplot(20,20,i+1)
            axis('off')
            imshow(img)
            i +=1
            if i == 400:
                figure()
                show()
                i = 0
        show()


class NameToIndex:
    def __init__(self):
        self.buf = {}
        self.names = []

    def map(self,name):
        if self.buf.has_key(name):
            return self.buf[name]
        else:
            id = len(self.names)
            self.buf[name] = id
            self.names.append(name)
            return id

    def name(self, id):
        return self.names[id]

class DocClassifier(AutoSave):
    '''
    文档分类
    '''
    def __init__(self,word_cluster):
        AutoSave.__init__(self)
        self._word_cluster = word_cluster

    def fit(self):

        wordids_map = NameToIndex()
        labs_map = NameToIndex()

        wordscount = self._word_cluster.get_words_count()
        print "start compute_tfidf ..."
        #计算文档的词袋模型
        docs = self._word_cluster.get_samples()
        count =0
        bow = []
        labs = []

        for k,v in docs.iteritems():
            vec = numpy.zeros(wordscount).tolist()
            for i in v:
                vec[wordids_map.map(i)]+=1
            bow.append(vec)
            labs.append(labs_map.map(k[0]))

        labs = numpy.array(labs)

        tfidf = TfidfTransformer(smooth_idf=True, sublinear_tf=True,use_idf=True)
        datas = numpy.array(tfidf.fit_transform(bow).toarray())

        print "compute_tfidf done"
        pca = RandomizedPCA(n_components=20, whiten=True).fit(datas)
        svc = train_svc(numpy.array(labs_map.names), labs, pca.transform(datas))

        self._tfidf = tfidf
        self._svc = svc
        self._labs_map = labs_map
        self._wordids_map = wordids_map
        self._pca = pca


    def predict(self, img_file):
        doc_words = self._word_cluster.predict(img_file)
        vec = numpy.zeros(self._word_cluster.get_words_count()).tolist()
        for i in doc_words:
            if i != -1:
                vec[self._wordids_map.map(i)]+=1

        tfidf = numpy.array(self._tfidf.fit_transform(vec).toarray())

        tfidf = self._pca.transform(tfidf)
        res = {}
        i=0
        for score in self._svc.predict_proba(tfidf)[0]:
            res[self._labs_map.names[i]] = score
            i+=1
        return res
