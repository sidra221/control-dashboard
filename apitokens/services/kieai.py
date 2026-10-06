"""تكامل مباشر مع kie.ai (بدون n8n) — مخصص لمسار الودجت التفاعلي حيث
الوقت الحقيقي مهم. نفس الـ API يلي جربناه بالـ n8n workflow، بس بايثون
مباشرة لتقليل زمن الاستجابة."""

import json
import time

import requests

UPLOAD_URL = "https://kieai.redpandaai.co/api/file-stream-upload"
CREATE_TASK_URL = "https://api.kie.ai/api/v1/jobs/createTask"
RECORD_INFO_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"

MODEL = "gpt-image-2-image-to-image"
DEFAULT_MAX_ATTEMPTS = 50
DEFAULT_DELAY_SECONDS = 5


class TryOnError(Exception):
    pass


def upload_image(api_key, file_obj, filename):
    response = requests.post(
        UPLOAD_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (filename, file_obj)},
        data={"uploadPath": "tryon"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise TryOnError(data.get("msg") or "upload_failed")
    return data["data"]["downloadUrl"]


def create_task(api_key, prompt, input_urls):
    response = requests.post(
        CREATE_TASK_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "input": {
                "prompt": prompt,
                "input_urls": input_urls,
                "aspect_ratio": "auto",
            },
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 200:
        raise TryOnError(data.get("msg") or "create_task_failed")
    return data["data"]["taskId"]


def poll_until_done(api_key, task_id, max_attempts=DEFAULT_MAX_ATTEMPTS, delay=DEFAULT_DELAY_SECONDS):
    for _ in range(max_attempts):
        time.sleep(delay)
        response = requests.get(
            RECORD_INFO_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            params={"taskId": task_id},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()["data"]
        state = data.get("state")
        if state == "success":
            result = json.loads(data["resultJson"])
            urls = result.get("resultUrls") or []
            if not urls:
                raise TryOnError("no_result_url")
            return urls[0]
        if state == "fail":
            raise TryOnError(data.get("failMsg") or "generation_failed")
    raise TryOnError("timed_out")


def generate_tryon(api_key, person_photo, person_filename, product_image_url, prompt):
    """بترفع صورة الزبون، بتبلّش مهمة توليد بصورتين (الزبون + المنتج)،
    وبتستنى لحد ما تخلص. برجّع رابط الصورة النهائية أو بترمي TryOnError.
    """
    person_url = upload_image(api_key, person_photo, person_filename)
    task_id = create_task(
        api_key,
        prompt=prompt,
        input_urls=[person_url, product_image_url],
    )
    return poll_until_done(api_key, task_id)
