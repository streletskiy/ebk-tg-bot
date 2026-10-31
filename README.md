# ebk-tg-bot

Telegram-бот для [ezBookkeeping](https://ezbookkeeping.mayswind.net/): расходы и доходы
из текста, чеков и голосовых сообщений, отчёты и остатки. Codex OAuth, локальный Whisper,
PostgreSQL, доступ по Telegram ID и обновляемый статус обработки.
Выписки: PDF Яндекс Банка и Т-Банка, CSV/XLSX Т-Банка (до 15 МБ, 100 страниц,
1000 операций; PDF с текстовым слоем). Пришлите файл, выберите `/import_account ID`.
Бот сверит дубли, предложит категории и повторно проверит решения через ChatGPT.
`/import` — план, `/import_page N` — строки, `/import_category N ID` — уточнить категорию,
`/import_skip N` — исключить строку. `/import_confirm КОД` подтверждает добавление новых
записей и уточнение времени однозначных совпадений. Спорные строки и переводы пропускаются;
категории, комментарии и вложения существующих записей сохраняются. `/cancel` отменяет план.
Память категорий: «Запомни: доход с Авито — продажа вещей». `/memory` — свои правила,
`/forget ID` — удалить; правила сохраняются между перезапусками.

## Запуск

1. Скопируйте `.env.example` в `.env`; укажите токен бота, URL ezBookkeeping и основной счёт.
2. Создайте `.local/data`. Сохраните API token ezBookkeeping в `ezbookkeeping.token`,
   случайный пароль PostgreSQL — в `postgres.password` внутри этой папки. Не публикуйте её.
3. Выполните:

```sh
docker compose pull
docker compose run --rm --user root --cap-add CHOWN --cap-add FOWNER --cap-add DAC_OVERRIDE --entrypoint sh bot -c 'chown -R 1000:1000 /data; chmod 700 /data'
docker compose run --rm bot pair
docker compose run --rm bot login
docker compose run --rm bot warmup
docker compose up -d
```

Откройте одноразовую ссылку из `pair`; контакты добавляются через `/allow ID Имя`.
Все контакты работают с общей книгой. `/help`, `/status`, `/retry` доступны в боте.
Данные PostgreSQL сохраняются в Docker volume; OAuth и настройки — в `.local/data`.

Образы: `ghcr.io/streletskiy/ebk-tg-bot:dev` — каждый коммит dev;
`:0.3.1` и `:latest` — опубликованные стабильные релизы. Поддерживаются amd64 и arm64.

При переходе со старой SQLite остановите бота, сохраните резервную копию и выполните
`docker compose run --rm bot migrate-sqlite --sqlite-path /data/moneybot.sqlite3`
до запуска нового бота. Импорт проверяет все строки и не перезаписывает непустую базу.
Базу ezBookkeeping эта миграция не изменяет.

Лицензия [MIT](LICENSE).
