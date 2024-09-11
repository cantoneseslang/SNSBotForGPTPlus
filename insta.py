import os
from io import BytesIO
import re
import random
from google.cloud import firestore
from google.cloud import storage
from datetime import datetime, time, timedelta
import pytz
from flask import Flask, request, render_template, session, redirect, url_for, jsonify
import unicodedata
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import requests
import tiktoken
from PIL import Image
import json
import uuid

from insta_functions import run_conversation

INSTA_ACCESS_TOKEN = os.getenv('INSTA_ACCESS_TOKEN')
INSTA_BUSINESS_ACCOUNT = os.getenv('INSTA_BUSINESS_ACCOUNT')

DATABASE_NAME = os.getenv('DATABASE_NAME')

# Firestore クライアントの初期化
try:
    db = firestore.Client(database=DATABASE_NAME)
except Exception as e:
    print(f"Error creating Firestore client: {e}")
    raise
 
def response_filter(bot_reply):
    pattern101 = r"\[.*\]\((https?://[^\]]+)\)"
    bot_reply = re.sub(pattern101, r" \1", bot_reply).strip()
    response = re.sub(r"\n{2,}", "\n", bot_reply)
    return response.rstrip('\n')

def overlay_transparent_image(base_image, overlay_image, position=(0, 0)):
    base_image.paste(overlay_image, position, overlay_image)
    return base_image

def get_image_with_retry(url, max_retries=3, backoff_factor=0.3):
    session = requests.Session()
    retries = Retry(total=max_retries, backoff_factor=backoff_factor, status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))

    try:
        response = session.get(url)
        response.raise_for_status()  # これにより、応答がHTTPエラーコードの場合に例外が発生します
        return Image.open(BytesIO(response.content))
    except requests.exceptions.RequestException as e:
        print(f"Error fetching image from {url}: {e}")
        return None

def basic_info():
    # Instagramの接続設定
    config = {
        "access_token": INSTA_ACCESS_TOKEN,
        "instagram_account_id": INSTA_BUSINESS_ACCOUNT,
        "graph_domain": 'https://graph.facebook.com/',
        "version": 'v18.0',
        "endpoint_base": 'https://graph.facebook.com/v18.0/'
    }
    return config

def InstaApiCall(url, params, request_type, files=None):
    # Instagram APIへのリクエスト（ファイル送信対応）
    if request_type == 'POST':
        if files:
            req = requests.post(url, data=params, files=files)
        else:
            req = requests.post(url, params)
    else:
        req = requests.get(url, params)
    return json.loads(req.content)

def createMedia(params, image_url):
    # メディア（画像）の作成
    url = params['endpoint_base'] + params['instagram_account_id'] + '/media'
    data = {
        'image_url': image_url,
        'caption': params['caption'],
        'access_token': params['access_token']
    }
    return InstaApiCall(url, data, 'POST')

def publishMedia(mediaObjectId, params):
    # メディア（画像）の公開
    url = params['endpoint_base'] + params['instagram_account_id'] + '/media_publish'
    data = {
        'creation_id': mediaObjectId,
        'access_token': params['access_token']
    }
    return InstaApiCall(url, data, 'POST')

def instagram_upload_image(params, image_url):
    # 画像をInstagramにアップロード
    media_response = createMedia(params, image_url)

    # media_responseの内容を確認
    print("Media Response:", media_response)

    # media_idの取得
    if 'id' in media_response:
        media_id = media_response['id']
        publish_response = publishMedia(media_id, params)
        return publish_response
    else:
        print("Failed to create media. Check the media_response for error details.")
        return None

def set_bucket_lifecycle(bucket_name, age):
    storage_client = storage.Client()
    bucket = storage_client.get_bucket(bucket_name)

    rule = {
        'action': {'type': 'Delete'},
        'condition': {'age': age}  # The number of days after object creation
    }
    
    bucket.lifecycle_rules = [rule]
    bucket.patch()
    return

def bucket_exists(bucket_name):
    """Check if a bucket exists."""
    storage_client = storage.Client()

    bucket = storage_client.bucket(bucket_name)

    return bucket.exists()

def upload_blob(bucket_name, source_stream, destination_blob_name, content_type='image/png'):
    """Uploads a file to the bucket from a byte stream."""
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(destination_blob_name)

        blob.upload_from_file(source_stream, content_type=content_type)
    
        public_url = f"https://storage.googleapis.com/{bucket_name}/{destination_blob_name}"
        return public_url
    except Exception as e:
        print(f"Failed to upload file: {e}")
        raise

def generate_insta(user_id, bot_reply, public_img_url=[]):
    r_bot_reply = bot_reply
    print(f"initiated insta. user ID: {user_id}, bot_reply: {bot_reply}, public_img_url: {public_img_url}")
    insta_system_prompt = INSTA_SYSTEM_PROMPT
    insta_overlay_url = INSTA_OVERLAY_URL
            
    # OpenAI API へのリクエスト
    messages_for_api = [
        {'role': 'system', 'content': insta_system_prompt}
    ]
        
    messages_for_api.append({'role': 'user', 'content': insta_order_prompt + "\n" + bot_reply})
    
    print(f"insta initiate run_conversation. messages_for_api: {messages_for_api}")
    response = run_conversation(AI_MODEL, messages_for_api)
    bot_reply = response.choices[0].message.content
    print(f"before filtered bot_reply: {bot_reply}")
    bot_reply = response_filter(bot_reply)
    print(f"insta bot_reply: {bot_reply}, public_img_url: {public_img_url}")
        
    if public_img_url:
        # Download image from URL
        base_img = get_image_with_retry(public_img_url)
        overlay_img = get_image_with_retry(insta_overlay_url)
        combined_img = overlay_transparent_image(base_img, overlay_img)
        # オーバーレイされた画像をアップロード
        img_data = BytesIO()
        combined_img.save(img_data, format='PNG')
        img_data.seek(0)

        print(f"BUCKET_NAME: {BUCKET_NAME}, FILE_AGE: {FILE_AGE} ")
        if bucket_exists(BUCKET_NAME):
            set_bucket_lifecycle(BUCKET_NAME, FILE_AGE)
        else:
            print(f"Bucket {BUCKET_NAME} does not exist.")
            return "SYSTEM:バケットが存在しません。", public_img_url, public_img_url_s
        filename = str(uuid.uuid4())
        print(f"filename: {filename}")
        blob_path = f'{user_id}/{filename}.png'
        print(f"blob_path: {blob_path}")
        public_img_url = upload_blob(BUCKET_NAME, img_data, blob_path)
        print(f"public_img_url: {public_img_url}")
 
        # 基本情報を設定
        params = basic_info()
        params['caption'] = bot_reply

        # 画像ファイルのパス
        # 画像のURL
        image_url = public_img_url


        # 画像をアップロード
        instagram_upload_image(params, image_url)
    else:
        print("Error: it has not include image URL.can not post instagram")
    return
