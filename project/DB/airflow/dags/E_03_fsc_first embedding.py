from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
import pandas as pd

from airflow import DAG
from airflow.operators.python_operator import PythonOperator
from elasticsearch import Elasticsearch, helpers
import re

from package.fsc_crawling import crawling
from package.fsc_extract import extract_main_content, extract_reason
from package.vector_embedding import generate_embedding

# Elasticsearch 인스턴스 생성 (Docker 내부에서 실행 중인 호스트에 연결)
es = Elasticsearch('http://host.docker.internal:9200')

# Elasticsearch 인덱스 생성 또는 재설정 함수
def create_or_update_index():
    """Elasticsearch 인덱스를 생성 또는 갱신하여 '날짜' 필드를 date 타입으로 설정"""
    # 인덱스가 이미 존재하면 삭제
    if es.indices.exists(index='raw_data'):
        es.indices.delete(index='raw_data')
        print("기존 인덱스 삭제 완료")
    # 파이프라인 설정
    pipeline_body = {
        "description": "text 필드를 content에 복사한다",
        "processors": [
            {
                "set": {
                    "field": "content",
                    "value": "{{text}}"
                }
            }
        ]
    }
    # 파이프라인 생성
    es.ingest.put_pipeline(id="copy_text_to_content_pipeline", body=pipeline_body)
    # 새로운 인덱스 생성 (날짜 필드를 date 타입으로 설정)
    # 인덱스 설정 및 매핑 구성
    index_body = {
        "settings": {
            "analysis": {
                "analyzer": {
                    "nori": {
                        "tokenizer": "nori_tokenizer"
                    }
                }
            }
        },
        "default_pipeline": "copy_text_to_content_pipeline",
        "mappings": {
            "properties": {
                "제목": {
                    "type": "text",
                    "analyzer": "nori"
                },
                "내용": {
                    "type": "text",
                    "analyzer": "nori"
                },
                "개정이유": {
                    "type": "text",
                    "analyzer": "nori"
                },
                "주요내용": {
                    "type": "text",
                    "analyzer": "nori"
                },
                "날짜": {
                    "type": "date"
                },
                "URL": {
                    "type": "keyword"
                },
                "vector": {
                    "type": "dense_vector",
                    "dims": 1536
                }
            }
        }
    }

    # 인덱스 생성
    index_name = "test_docs"
    if not es.indices.exists(index=index_name):
        es.indices.create(index=index_name, body=index_body)
        print(f"Index '{index_name}' created successfully.")
    else:
        print(f"Index '{index_name}' already exists.")


def crawling_extract_df():
    df = crawling(10)
    
    if df is None or df.empty:
        print("크롤링된 데이터가 없습니다. 빈 DataFrame을 반환합니다.")
        return pd.DataFrame(columns=['제목', '날짜', 'URL', '내용', '개정이유', '주요내용'])

    df['내용'] = df['내용'].fillna('내용없음')
    df['개정이유'] = df['내용'].apply(extract_reason)
    df['주요내용'] = df['내용'].apply(extract_main_content)
    df['날짜'] = pd.to_datetime(df['날짜'], errors='coerce').dt.strftime('%Y-%m-%d')  # 날짜 형식 통일

    return df


def upload_data():
    df = crawling_extract_df()
    
    actions = [
        {
            "_op_type": "index",
            "_index": "raw_data",
            "_id": f"{row['제목']}_{row['날짜']}",
            "_source": {
                "제목": row['제목'],
                "날짜": row['날짜'],
                "URL": row['URL'],
                "내용": row['내용'],
                "개정이유": row['개정이유'],
                "주요내용": row['주요내용'],
            }
        }
        for _, row in df.iterrows()
    ]

    print(f"삽입할 데이터 수: {len(actions)}")
    
    if actions:
        helpers.bulk(es, actions)
        print(f"{len(actions)}개의 데이터를 업로드했습니다.")
    else:
        print("업로드할 데이터가 없습니다.")


# Airflow DAG 기본 설정
default_args = {
    'depends_on_past': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
}

# Airflow DAG 정의
with DAG(
    'fsc_first_embedding',
    default_args=default_args,
    description="입법예고/규정변경예고 데이터를 Elasticsearch에 저장합니다.",
    schedule_interval='@monthly',
    start_date=datetime.now(),
    catchup=False,
    tags=['elasticsearch', 'crawl', 'finance']
) as dag:

    # Elasticsearch 인덱스 초기화 작업
    initialize_index = PythonOperator(
        task_id="initialize_elasticsearch_index",
        python_callable=create_or_update_index,
    )

    # 데이터 업로드 작업 정의
    upload_task = PythonOperator(
        task_id="upload_data_to_elasticsearch",
        python_callable=upload_data,
    )

    # DAG 실행 순서 설정
    initialize_index >> upload_task