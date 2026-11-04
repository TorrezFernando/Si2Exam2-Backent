import math
from pathlib import Path
import numpy as np
import pandas as pd
import joblib
from faker import Faker
from sklearn.ensemble import RandomForestRegressor
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import r2_score,mean_absolute_error,mean_squared_error,silhouette_score


def synthetic(seed=42):
    rng=np.random.default_rng(seed);fake=Faker('es_ES');fake.seed_instance(seed)
    rows=[]
    from datetime import date
    end=pd.Timestamp(date.today()).replace(day=1)-pd.offsets.MonthBegin(1)
    for month in pd.date_range(end=end,periods=8,freq='MS'):
        for product in range(1,25):
            for branch in (1,2):
                category=1+(product%3)
                quantity=max(1,int(20+product*2+branch*3+month.month*2+rng.normal(0,3)))
                rows.append(dict(date=month.date().isoformat(),branch_id=branch,category_id=category,product_id=product,customer=fake.name(),quantity=quantity,revenue=quantity*(30+product*5)))
    return rows


def analyze(rows,tenant_id,branch=None,demo=False):
    from threadpoolctl import threadpool_limits
    with threadpool_limits(limits=1):
        return _analyze(rows,tenant_id,branch,demo)


def _analyze(rows,tenant_id,branch=None,demo=False):
    if len(rows)<30:raise ValueError('Se requieren al menos 30 detalles y 8 meses con ventas para entrenar; puedes usar la demostración sintética.')
    frame=pd.DataFrame(rows);frame['date']=pd.to_datetime(frame['date']);frame['month']=frame['date'].dt.to_period('M')
    monthly=frame.groupby(['month','branch_id','category_id'],as_index=False)['quantity'].sum().sort_values('month')
    months=sorted(monthly['month'].unique())
    if len(months)<8:raise ValueError('Historial insuficiente: se requieren 8 meses diferentes con ventas.')
    def features(data):
        return pd.DataFrame({'trend':data['month'].map(lambda p:p.year*12+p.month),'sin':data['month'].map(lambda p:math.sin(2*math.pi*p.month/12)),'cos':data['month'].map(lambda p:math.cos(2*math.pi*p.month/12)),'branch':data.branch_id,'category':data.category_id})
    cutoff=months[max(1,int(len(months)*0.8))];train=monthly.month<cutoff;test=~train
    model=RandomForestRegressor(n_estimators=100,min_samples_leaf=2,random_state=42,n_jobs=1)
    model.fit(features(monthly[train]),monthly.loc[train,'quantity']);pred=model.predict(features(monthly[test]));truth=monthly.loc[test,'quantity']
    metrics={'r2':float(r2_score(truth,pred)) if len(truth)>1 else None,'mae':float(mean_absolute_error(truth,pred)),'rmse':float(mean_squared_error(truth,pred)**0.5),'train_rows':int(train.sum()),'test_rows':int(test.sum()),'split':'cronológico, últimos 20% de meses'}
    model.fit(features(monthly),monthly.quantity)
    future=monthly[['branch_id','category_id']].drop_duplicates().copy();future['month']=months[-1]+1
    forecast=[dict(branch_id=int(r.branch_id),category_id=int(r.category_id),month=str(r.month),units=round(max(0,float(p)),2)) for r,p in zip(future.itertuples(),model.predict(features(future)))]
    products=frame.groupby('product_id').agg(quantity=('quantity','sum'),revenue=('revenue','sum'),active_months=('month','nunique')).reset_index()
    matrix=StandardScaler().fit_transform(products[['quantity','revenue','active_months']])
    unique=len(np.unique(matrix,axis=0));k=min(3,len(products)-1,unique)
    if k<2:raise ValueError('Se requieren al menos 3 productos con perfiles diferentes para segmentar.')
    cluster=KMeans(n_clusters=k,random_state=42,n_init=10);labels=cluster.fit_predict(matrix)
    score=float(silhouette_score(matrix,labels,sample_size=min(2000,len(matrix)),random_state=42)) if 1<len(set(labels))<len(matrix) else None
    segments=[dict(product_id=int(r.product_id),units=int(r.quantity),revenue=round(float(r.revenue),2),cluster=int(label)) for r,label in zip(products.itertuples(),labels)]
    folder=Path(__file__).resolve().parent/'artifacts'/f'tenant_{int(tenant_id)}';folder.mkdir(parents=True,exist_ok=True)
    # Unique immutable filenames avoid concurrent writes and never load user files.
    from uuid import uuid4
    artifact=f'{"demo" if demo else "real"}_branch_{branch or "all"}_{uuid4().hex}.joblib'
    joblib.dump({'model':model,'cluster':cluster,'metrics':metrics,'scope':{'tenant':tenant_id,'branch':branch},'source':'synthetic' if demo else 'real'},folder/artifact)
    return {'source':'synthetic' if demo else 'real','warning':'Demostración: no representa resultados de la empresa.' if demo else 'Estimaciones; no garantizan demanda futura. Meses sin operaciones no se imputan como ventas cero.','metrics':metrics,'silhouette':score,'forecast':forecast,'segments':segments,'artifact':artifact}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Generar historial sintético sin modificar la base comercial.')
    parser.add_argument('--output',required=True)
    args=parser.parse_args();pd.DataFrame(synthetic()).to_csv(args.output,index=False)
