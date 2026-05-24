"""
Test script for laozhang API image generation.
Usage: python test_laozhang_image.py
"""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI

# ─────────────────────────── Configuration ───────────────────────────

PROMPT = """
нужно создать изображение. сделай комикс, удели внимание диалогам и персонажам. Зрители должны знать кто есть кто. Принадлежность реплик персонажей должна быть соблюдена:

📅 Ежедневное саммари

📌 Квест «найди своих у сцены»
Сначала ловили интернет и друг друга: Арсен был «чуть справа от сцены», Алина с Камилем — у шариков, потом народ устал от караоке и ушёл по набережной. Дальше были мост, монумент, бинокль, смотровая и классика жанра: «мы тут», «вы где», «мы светим фонариком». ✅ Камиль в итоге закрыл прогулку красиво: всем спасибо, погуляли классно 🔥

💸 Big Term пришёл вовремя как всегда
Big Term появился уже после основного движняка и предложил через сорок минут погулять в центре — Карина сразу выдала: «вовремя как всегда». Потом внезапно всплыли апишки, логотипы и вопрос «а это бесплатно?», на что Карина голосом включила режим кассы: плати бабки. Big Term попытался спорить, но Карина напомнила про овнов — и спор умер сам, без лишних жертв 😅

🚲 Илья проехал и не заметил Влада
Vladislav пожаловался, что Илья пролетел на велике и даже внимания не обратил: всё, зазвездился. Илья оправдывался маршрутом домой и уставшей трансмиссией после героического покорения горы у телецентра. Параллельно Диана подняла вечную тему «грех не здороваться», а Карина напомнила, что сама Диана на тайном Санте тоже не без греха.

🧺 Пикник, комары и ванна из пантенола
Карина захотела сходку-пикник с чаем, Камиль поддержал, Buba уже кинул вариант про ПВД и Хазинское ущелье. ❓ Конкретики по дате и месту нет: телецентр тоже мелькал, но пока это больше «надо-надо». Заодно вспомнили комаров у Уфа Арены, которые «любят вкусную кровь», а Илья после прогулки собрался принимать ванну из пантенола.

🌲 Дипломные рамки, хвойные шишечки и оса на ужин
Buba страдал с рамкой для диплома: у группаша одно, у него другое, всем будто насрать — Карина советовала шаблон и Word, но ❓ вопрос явно не закрылся. Потом ушли в ботсад: хвойные цветут или нет, лиственница это или можжевельник, розовые шишечки и «я дальтоник, мне не стоит доверять». А под конец Telegram решил сам отправлять отменённые фотки, на кухне общаги улетела здоровая оса, и чат мгновенно превратил её в «упущенный ужин» и «белок».

Настроение было максимально прогулочное: кто-то ищет людей фонариком, кто-то спорит с овном, кто-то ловит осу как источник протеина. Всё как обычно: хаос, смешки и немного бытовой героики.
"""

# Optional settings
MODEL = "gpt-image-2"
SIZE = "auto"
QUALITY = "high"
OUTPUT_FORMAT = "png"

# ─────────────────────────── Script ───────────────────────────

def main() -> None:
    # Load API key from .env
    load_dotenv()
    api_key = os.getenv("LAOZHANG_API_KEY")
    if not api_key:
        raise RuntimeError("LAOZHANG_API_KEY not found in .env file")

    # Initialize client
    client = OpenAI(
        api_key=api_key,
        base_url="https://api.laozhang.ai/v1"
    )

    # Generate image
    print(f"Generating image with model: {MODEL}")
    print(f"Prompt: {PROMPT[:100]}...")

    response = client.images.generate(
        model=MODEL,
        prompt=PROMPT,
        n=1,
        size=SIZE,
        quality=QUALITY,
        response_format="url"
    )

    # Validate response structure
    if not hasattr(response, "data") or response.data is None:
        raise RuntimeError(f"Response.data is None or missing. Full response: {response}")

    if len(response.data) == 0:
        raise RuntimeError(f"Response.data is empty. Full response: {response}")

    first_item = response.data[0]
    if first_item is None:
        raise RuntimeError(f"Response.data[0] is None. Full response: {response}")

    image_url = first_item.url
    if not image_url:
        raise RuntimeError(f"Image URL is empty or None. Full response: {response}")

    print(f"Image URL: {image_url}")

    # Download and save image
    img_response = requests.get(image_url)
    img_response.raise_for_status()

    script_dir = Path(__file__).parent
    output_path = script_dir / "generated_image.png"
    output_path.write_bytes(img_response.content)

    print(f"Image saved to: {output_path}")


if __name__ == "__main__":
    main()