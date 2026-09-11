# Telegram Music Bot

Telegram-бот на `aiogram`, який шукає та завантажує медіа через `yt-dlp`,
обробляє його за допомогою FFmpeg і зберігає стан у SQLite.

## Важливо перед запуском

1. Відкличте старий токен у BotFather командою `/revoke` і створіть новий.
2. Скопіюйте `.env.example` у `.env` та заповніть новий `BOT_TOKEN`.
3. Додайте бота адміністратором у канали з правом публікувати повідомлення.
4. Не додавайте `.env`, файли `*.db`, логи або завантажені медіа в Git.

## Локальний запуск

Потрібні Python 3.13+, FFmpeg/FFprobe та Deno у `PATH`.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python verify_install.py
.\.venv\Scripts\python bot.py
```

## Docker

Усі змінні з `.env.example`, що містять шлях `/data`, мають залишатися такими
в контейнері. Том `bot-data` зберігає SQLite та медіафайли між перезапусками.

```bash
docker build -t telegram-music-bot .
docker volume create bot-data
docker run -d --name telegram-music-bot --restart unless-stopped \
  --env-file .env -v bot-data:/data telegram-music-bot
```

## Рекомендований безкоштовний хостинг

Для цієї архітектури найкраще підходить безкоштовна Linux VM з постійним
диском, насамперед Oracle Cloud Always Free Ampere A1. Боту потрібні:

- постійний фоновий процес для Telegram long polling;
- FFmpeg і Deno як системні програми;
- постійний диск для трьох SQLite-баз і медіакешу;
- достатньо RAM/CPU для паралельних `yt-dlp` та FFmpeg задач.

Безкоштовні web-сервіси Render і Koyeb для поточної версії не підходять:
вони засинають без вхідного HTTP-трафіку, а їхня безкоштовна локальна файлова
система не є постійною. Бот працює через вихідний long polling та не має HTTP
endpoint, який міг би коректно підтримувати web-сервіс активним.

### Розгортання на Ubuntu VM

```bash
sudo apt-get update
sudo apt-get install -y docker.io git
sudo systemctl enable --now docker
git clone YOUR_PRIVATE_REPOSITORY_URL music-bot
cd music-bot
cp .env.example .env
nano .env
sudo docker build -t telegram-music-bot .
sudo docker volume create bot-data
sudo docker run -d --name telegram-music-bot --restart unless-stopped \
  --env-file .env -v bot-data:/data telegram-music-bot
sudo docker logs -f telegram-music-bot
```

Не відкривайте вхідні порти у firewall/security list: для long polling вони
не потрібні. Потрібен лише вихідний HTTPS-доступ.

