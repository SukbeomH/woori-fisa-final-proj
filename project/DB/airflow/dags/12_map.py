from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime, timedelta
import requests
from shapely.geometry import shape
from bs4 import BeautifulSoup
import pandas as pd
import os
from opensearchpy import OpenSearch, helpers
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Set OpenSearch connection details
host = os.getenv("HOST")
port = os.getenv("PORT")
auth = (os.getenv("OPENSEARCH_ID"), os.getenv("OPENSEARCH_PASSWORD"))  # For testing only. Don't store credentials in code.

client = OpenSearch(
    hosts=[{'host': host, 'port': port}],
    http_auth=auth,
    use_ssl=True,
    verify_certs=False
)


# GeoJSON 데이터를 로드하고 국가별 중심 좌표를 추출하는 함수
def load_geojson_and_extract_centroids():
    geojson_url = "https://raw.githubusercontent.com/johan/world.geo.json/master/countries.geo.json"
    response = requests.get(geojson_url)
    if response.status_code == 200:
        geojson_data = response.json()
        centroids = {}
        for feature in geojson_data["features"]:
            country_name = feature["properties"]["name"]  # 국가 이름
            geometry = shape(feature["geometry"])  # 경계 정보
            centroid = geometry.centroid
            centroids[country_name] = {"lat": centroid.y, "lon": centroid.x}  # 중심 좌표
        return centroids
    else:
        print(f"GeoJSON 데이터를 가져오지 못했습니다. 상태 코드: {response.status_code}")
        return None

# 좌표 데이터를 캐싱
centroids_cache = load_geojson_and_extract_centroids()



# Define index name and setup
index_name = "travel_cautions"

def setup_index():
    if not client.indices.exists(index=index_name):
        client.indices.delete(index=index_name)
        print(f"Existing index '{index_name}' deleted.")
        mapping = {
            "mappings": {
                "properties": {
                    "Country": {"type": "keyword"},
                    "Travel_Caution": {"type": "boolean"},
                    "Travel_Restriction": {"type": "boolean"},
                    "Departure_Advisory": {"type": "boolean"},
                    "Travel_Ban": {"type": "boolean"},
                    "Special_Travel_Advisory": {"type": "boolean"}
                }
            }
        }
        client.indices.create(index=index_name, body=mapping)
        print(f"Index '{index_name}' created with mapping.")
    else:
        print(f"Index '{index_name}' already exists.")

def fetch_data():
    url = "https://www.0404.go.kr/dev/country.mofa?idx=&hash=&chkvalue=no1&stext=&group_idx="
    response = requests.get(url)
    soup = BeautifulSoup(response.text, "html.parser")
    advice_levels = ["Travel_Caution", "Travel_Restriction", "Departure_Advisory", "Travel_Ban", "Special_Travel_Advisory"]
    data = []
    countries = soup.select("ul.country_list > li")
    for country in countries:
        country_name = country.select_one("a").text.strip()
        img_tags = country.select("img")
        travel_advice = [img["alt"].strip() for img in img_tags if img.get("alt")]
        advice_flags = {level: True if level in travel_advice else False for level in advice_levels}
        advice_flags = {"Country": country_name, **advice_flags}
        data.append(advice_flags)
    return pd.DataFrame(data)

def upload_data_with_coordinates():
    df = fetch_data()
    if not centroids_cache:
        print("좌표 데이터를 로드하지 못했습니다.")
        return

    actions = []
    for _, row in df.iterrows():
        country_name = row["Country"]
        coordinates = centroids_cache.get(country_name, {"lat": None, "lon": None})
        actions.append({
            "_op_type": "index",
            "_index": index_name,
            "_source": {
                "Country": country_name,
                "Coordinates": coordinates,
                "Travel_Caution": row["Travel_Caution"],
                "Travel_Restriction": row["Travel_Restriction"],
                "Departure_Advisory": row["Departure_Advisory"],
                "Travel_Ban": row["Travel_Ban"],
                "Special_Travel_Advisory": row["Special_Travel_Advisory"]
            }
        })
    
    if actions:
        helpers.bulk(client, actions)
        print(f"{len(actions)}개의 데이터를 업로드했습니다.")
    else:
        print("업로드할 데이터가 없습니다.")


default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "start_date": datetime(2024, 11, 13),
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    "daily_travel_cautions_dag",
    default_args=default_args,
    description="Fetch and upload travel cautions daily with coordinates",
    schedule_interval="0 0 * * *",
    catchup=False,
) as dag:

    setup_index_task = PythonOperator(
        task_id="setup_index",
        python_callable=setup_index
    )

    upload_data_with_coordinates_task = PythonOperator(
        task_id="upload_data_with_coordinates",
        python_callable=upload_data_with_coordinates
    )

    setup_index_task >> upload_data_with_coordinates_task