
# CineplexBD Ticket Watcher

## 🚀 Quick Start (Windows)

1. Download the latest release:
   👉 https://github.com/SalsabilaZaman/Ticket-Watcher/releases

2. Extract the ZIP file

3. Double-click:
   **TicketWatcher.exe**

4. Follow the on-screen setup

That's it!

---

## 📦 Download & Run

If you want to run from source, see below.

---

## 🧠 What This Does

A Python tool to monitor CineplexBD for new movie ticket availability and get instant notifications via desktop and Telegram.

---

## ✨ Features
- Monitors movie ticket availability at CineplexBD
- Sends notifications via Windows toast and Telegram
- Web dashboard for live status
- Easy CLI setup for movie/location

---

## ⚙️ Setup (Advanced Users)

### Requirements
- Python 3.10+
- Windows, macOS, or Linux (desktop notifications work on all three)
- Telegram account (for Telegram notifications)

### Installation

1. **Clone the repository**
   ```sh
   git clone https://github.com/SalsabilaZaman/Ticket-Watcher.git
   cd "Ticket Watcher"
   ```

2. **Install dependencies**

   Windows:
   ```sh
   install.bat
   ```

   macOS / Linux:
   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   ```

   Playwright is used for the guest-login token and as a fallback scraper, so
   the chromium download is required even though monitoring uses the JSON API.

---

### Configuration

1. **Run interactive setup**
   ```sh
   python main.py setup  # Only for developers
   # Or manually:
   python main.py setup

2. **Edit config.yaml** (optional)
   - Adjust monitoring interval, notification settings, etc.

3. **Configure Telegram notifications** (optional)
   - [Create a Telegram bot](https://core.telegram.org/bots#6-botfather)
   - Get your `TELEGRAM_BOT_TOKEN` from BotFather
   - Get your `TELEGRAM_CHAT_ID` (see below)
   - Create a `.env` file in the project root:
     ```env
     TELEGRAM_BOT_TOKEN=your_bot_token_here
     TELEGRAM_CHAT_ID=your_chat_id_here
     ```
   - In `config.yaml`, set `notifications.telegram.enabled: true`

#### How to get your Telegram Chat ID
1. Start a chat with your bot on Telegram.
2. Visit: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
3. Send a message to your bot, then refresh the above URL.
4. Find `chat":{"id":<YOUR_CHAT_ID>}` in the JSON response.

---

## ▶️ Usage

- **Start monitoring:**
  ```sh
  python main.py watch
  ```
- **Show available locations:**
  ```sh
  python main.py list-locations
  ```
- **Show movies at a location:**
  ```sh
  python main.py list-movies <LOCATION_ID>
  ```
- **Test notifications:**
  ```sh
  python main.py test-notify
  ```
- **Web dashboard:**
  ```sh
  python main.py dashboard
  # or specify port
  python main.py dashboard --port 8080
  ```

---

## Troubleshooting
- **Playwright errors:** Run `playwright install` again.
- **Telegram not working:** Double-check your bot token, chat ID, and `.env` file.
- **Desktop notifications:** Windows uses `winotify`, macOS uses Notification Center
  (install `terminal-notifier` for clickable alerts), Linux uses `notify-send`.

---

## License
MIT
