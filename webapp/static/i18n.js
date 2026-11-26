/* Русский и узбекский (латиница). Нет перевода — берётся русский. */
(function () {
  'use strict';
  const ru = {
    sum: 'сум', hello: 'Привет, {name}', ready: 'Кимо готов', kimo_with_you: 'Ваш помощник на связи', all_calm: 'Все системы в норме',
    agents_busy: 'Работают агенты: {n}', agents_online: 'На связи {n} из {m}', today_tasks: 'Задачи на сегодня', today_money: 'Деньги сегодня',
    business_chat: 'Бизнес-чат', biz_connected: 'Подключён, Кимо отвечает клиентам', biz_not_connected: 'Не подключён', biz_how: 'Telegram → Настройки → Telegram Business → Чат-боты → Кимо',
    ai_advice: 'AI-рекомендации', ai_advice_hint: 'Советы по расходам, доходам и задачам', assistant: 'Ассистент', assistant_hello: 'Скажите, что сделать: добавить задачу, расход или доход, показать статистику.',
    assistant_ph: 'Напишите команду…', send: 'Отправить', nav_home: 'Главная', nav_tasks: 'Задачи', nav_stats: 'Аналитика', nav_business: 'Бизнес', nav_more: 'Ещё',
    tasks: 'Задачи', today: 'Сегодня', tomorrow: 'Завтра', no_tasks: 'Задач нет. Нажмите «+», чтобы добавить.', done: 'Выполнено', delete: 'Удалить', add: 'Добавить', added: 'Добавлено', deleted: 'Удалено',
    stats_tasks: 'Выполнение за 14 дней', done_n: 'Сделано: {n}', undone_n: 'Не сделано: {n}', streak: 'Серия: {n} дн.', done_of: '{a} из {b} задач',
    new_task: 'Новая задача', new_reminder: 'Новое напоминание', what_to_do: 'Что сделать', text: 'Текст', date: 'Дата', time: 'Время', optional: 'необязательно', need_text: 'Введите текст задачи', need_text_time: 'Введите текст и время',
    analytics: 'Аналитика', g_day: 'По дням', g_week: 'По неделям', g_month: 'По месяцам', income: 'Доходы', expense: 'Расходы', balance: 'Баланс', dynamics: 'Динамика', categories: 'Категории',
    task_progress: 'Успеваемость по задачам (30 дней)', usd_rate: 'Курс доллара', recent_ops: 'Последние операции', no_data: 'Данных пока нет', new_operation: 'Новая операция',
    amount: 'Сумма', category: 'Категория', note: 'Заметка', save: 'Сохранить', saved: 'Сохранено', need_amount: 'Введите сумму больше нуля',
    reminders: 'Напоминания', no_reminders: 'Напоминаний нет', advice_daily: 'Обновляются раз в сутки', advice_stale: 'ИИ сейчас недоступен, показаны советы от {d}', advice_empty: 'Добавьте несколько расходов, доходов или задач, и здесь появятся советы.', advice_unavailable: 'Советы временно недоступны, попробуйте позже.',
    agents: 'Агенты', agents_hint: 'Включение и выключение подтверждается в Telegram.', heartbeat: 'Heartbeat', confirm_in_tg: 'Подтвердите в Telegram', new_agent: 'Новый агент', agent_flow: 'Опишите, что должен делать агент. Его создаст Мимо, а токен из BotFather и права вы введёте и подтвердите в Telegram.', agent_describe: 'Описание агента', agent_placeholder: 'Например: агент, который отвечает на вопросы о заказах магазина и не называет скидки', create_via_mimo: 'Создать через Мимо', need_longer: 'Опишите агента подробнее (от 10 символов)',
    st_online: 'на связи', st_busy: 'занят', st_error: 'ошибка', st_offline: 'не на связи', st_ok: 'работает', st_warn: 'внимание', st_bad: 'сбой',
    connection: 'Подключение', ai_answers: 'Кимо отвечает клиентам', ai_answers_hint: 'Выключите, чтобы отвечать самому', ai_on: 'Кимо отвечает', ai_off: 'Кимо молчит', rules: 'Правила', r_prompt: 'Правила и инструкции', r_info: 'О бизнесе (MilliyPC)', r_ban: 'Запреты', r_tone: 'Тон общения', r_name: 'Имя владельца', r_pause: 'Пауза после вашего ответа, часов', r_flood: 'Ответов на чат в минуту',
    chats: 'Чаты клиентов', no_chats: 'Чатов пока нет: они появятся после первого сообщения клиента.', paused: 'пауза', dialog_log: 'Журнал диалога', role_client: 'клиент', role_assistant: 'Кимо', role_owner: 'вы',
    broadcasts: 'Рассылки', bc_rules: 'Кимо пишет только тем, кто сам писал вам или подписался по ссылке. Не больше одного сообщения в сутки на чат. Слово «стоп» отключает рассылки.', no_broadcasts: 'Рассылок нет. Нажмите «+».',
    via_bot: 'через бота', new_broadcast: 'Новая рассылка', edit_broadcast: 'Рассылка', chat: 'Чат', manual_id: 'Или введите ID вручную', or_manual: 'Другой чат (ID)', days: 'Дни недели', mode_text: 'Текст', mode_ai: 'ИИ по теме', message: 'Сообщение', topic: 'Тема', topic_placeholder: 'Например: спроси, как дела',
    bc_window: 'Через Business Telegram разрешает писать только в течение 24 часов после сообщения клиента. Иначе Кимо пришлёт вам готовый текст. Для ежедневных сообщений подписывайте людей по ссылке.', need_days: 'Выберите хотя бы один день',
    sub_link: 'Ссылка для подписки', sub_link_hint: 'Человек сам нажимает «Запустить», после этого Кимо пишет от имени бота без 24-часового окна.', copy: 'Копировать', copied: 'Скопировано', history: 'История отправок',
    d_daily: 'каждый день', d_weekdays: 'по будням', d_weekend: 'по выходным', wd1: 'пн', wd2: 'вт', wd3: 'ср', wd4: 'чт', wd5: 'пт', wd6: 'сб', wd7: 'вс',
    access: 'Доступы', no_grants: 'Выданных доступов нет', until: 'до', revoke: 'Отозвать', revoked: 'Доступ отозван', grant_access: 'Выдать доступ', user_id: 'Telegram ID', days_n: 'На сколько дней', d_short: 'дн.', grant: 'Выдать', granted: 'Доступ выдан', need_id_days: 'Введите ID и число дней',
    settings: 'Настройки', accent: 'Цвет акцента', custom_color: 'Свой цвет', language: 'Язык', timezone: 'Часовой пояс', max_per_day: 'Сообщений в сутки на чат', your_access: 'Ваш доступ', role_admin: 'Администратор', role_user: 'Пользователь',
    payments: 'Оплата', flags_hint: 'Включаются флагами в .env (SUBSCRIPTION_ENABLED, CLICK_ENABLED).', tokens: 'Токены и ключи', not_set: 'не задан', tokens_hint: 'Значения скрыты и не передаются в приложение.',
    more: 'Ещё', m_reminders: 'Ближайшие напоминания', m_advice: 'Советы на сегодня', m_broadcasts: 'Сообщения клиентам по расписанию', m_agents: 'Статус, включение, новый агент', m_access: 'Выдать и отозвать доступ', m_settings: 'Цвет, язык, часовой пояс', m_assistant: 'Команды текстом',
    bs_sent: 'Отправлено', bs_manual_needed: 'Нужна ручная отправка', bs_skipped: 'Пропущено', bs_failed: 'Не удалось', bs_stopped: 'Остановлено', bs_blocked: 'Заблокировано', bs_missed: 'Пропущено по времени', bs_retry: 'Повтор',
    open_in_tg: 'Откройте из Telegram', open_in_tg_hint: 'Это приложение работает только внутри Telegram.', no_access: 'Доступ не открыт', no_access_hint: 'Отправьте этот ID владельцу, и он откроет доступ.', retry: 'Повторить',
    err_generic: 'Что-то пошло не так', err_network: 'Нет связи с сервером', no_rights: 'Эта функция вам недоступна', never: 'никогда', just_now: 'только что', min_ago: '{n} мин назад', h_ago: '{n} ч назад', d_ago: '{n} дн. назад',
  };
  const uz = {
    sum: "so'm", hello: 'Salom, {name}', ready: 'Kimo tayyor', kimo_with_you: 'Yordamchingiz aloqada', all_calm: 'Barcha tizimlar me\'yorda',
    agents_busy: 'Ishlayotgan agentlar: {n}', agents_online: 'Aloqada {n}/{m}', today_tasks: 'Bugungi vazifalar', today_money: 'Bugungi pul',
    business_chat: 'Biznes-chat', biz_connected: 'Ulangan, Kimo mijozlarga javob beradi', biz_not_connected: 'Ulanmagan', biz_how: 'Telegram → Sozlamalar → Telegram Business → Chat-botlar → Kimo',
    ai_advice: 'AI tavsiya', ai_advice_hint: 'Xarajat, daromad va vazifalar bo\'yicha maslahatlar', assistant: 'Yordamchi', assistant_hello: 'Nima qilish kerakligini ayting: vazifa, xarajat yoki daromad qo\'shish, statistika ko\'rsatish.',
    assistant_ph: 'Buyruq yozing…', send: 'Yuborish', nav_home: 'Bosh sahifa', nav_tasks: 'Vazifalar', nav_stats: 'Tahlil', nav_business: 'Biznes', nav_more: 'Yana',
    tasks: 'Vazifalar', today: 'Bugun', tomorrow: 'Ertaga', no_tasks: 'Vazifalar yo\'q. Qo\'shish uchun «+» bosing.', done: 'Bajarildi', delete: 'O\'chirish', add: 'Qo\'shish', added: 'Qo\'shildi', deleted: 'O\'chirildi',
    stats_tasks: '14 kunlik bajarilish', done_n: 'Bajarildi: {n}', undone_n: 'Bajarilmadi: {n}', streak: 'Ketma-ket: {n} kun', done_of: '{b} tadan {a} ta vazifa',
    new_task: 'Yangi vazifa', new_reminder: 'Yangi eslatma', what_to_do: 'Nima qilish kerak', text: 'Matn', date: 'Sana', time: 'Vaqt', optional: 'ixtiyoriy', need_text: 'Vazifa matnini kiriting', need_text_time: 'Matn va vaqtni kiriting',
    analytics: 'Tahlil', g_day: 'Kunlar', g_week: 'Haftalar', g_month: 'Oylar', income: 'Daromad', expense: 'Xarajat', balance: 'Balans', dynamics: 'Dinamika', categories: 'Kategoriyalar',
    task_progress: 'Vazifalar bo\'yicha samaradorlik (30 kun)', usd_rate: 'Dollar kursi', recent_ops: 'So\'nggi amallar', no_data: 'Hozircha ma\'lumot yo\'q', new_operation: 'Yangi amal',
    amount: 'Summa', category: 'Kategoriya', note: 'Izoh', save: 'Saqlash', saved: 'Saqlandi', need_amount: 'Noldan katta summa kiriting',
    reminders: 'Eslatmalar', no_reminders: 'Eslatmalar yo\'q', advice_daily: 'Kuniga bir marta yangilanadi', advice_stale: 'AI hozir mavjud emas, {d} sanadagi maslahatlar ko\'rsatilmoqda', advice_empty: 'Bir nechta xarajat, daromad yoki vazifa qo\'shing, shunda maslahatlar paydo bo\'ladi.', advice_unavailable: 'Maslahatlar vaqtincha mavjud emas, keyinroq urinib ko\'ring.',
    agents: 'Agentlar', agents_hint: 'Yoqish va o\'chirish Telegramda tasdiqlanadi.', heartbeat: 'Heartbeat', confirm_in_tg: 'Telegramda tasdiqlang', new_agent: 'Yangi agent', agent_flow: 'Agent nima qilishini yozing. Uni Mimo yaratadi, BotFather tokenini va huquqlarni Telegramda o\'zingiz kiritasiz va tasdiqlaysiz.', agent_describe: 'Agent tavsifi', agent_placeholder: 'Masalan: do\'kon buyurtmalari haqidagi savollarga javob beradigan va chegirma aytmaydigan agent', create_via_mimo: 'Mimo orqali yaratish', need_longer: 'Agentni batafsilroq yozing (kamida 10 belgi)',
    st_online: 'aloqada', st_busy: 'band', st_error: 'xato', st_offline: 'aloqada emas', st_ok: 'ishlayapti', st_warn: 'diqqat', st_bad: 'nosozlik',
    connection: 'Ulanish', ai_answers: 'Kimo mijozlarga javob beradi', ai_answers_hint: 'O\'zingiz javob berish uchun o\'chiring', ai_on: 'Kimo javob beradi', ai_off: 'Kimo jim', rules: 'Qoidalar', r_prompt: 'Qoidalar va ko\'rsatmalar', r_info: 'Biznes haqida (MilliyPC)', r_ban: 'Taqiqlar', r_tone: 'Muloqot ohangi', r_name: 'Egasining ismi', r_pause: 'Javobingizdan keyingi pauza, soat', r_flood: 'Chatga daqiqada javoblar',
    chats: 'Mijoz chatlari', no_chats: 'Chatlar hali yo\'q: mijoz birinchi xabar yozgach paydo bo\'ladi.', paused: 'pauza', dialog_log: 'Suhbat jurnali', role_client: 'mijoz', role_assistant: 'Kimo', role_owner: 'siz',
    broadcasts: 'Xabarnomalar', bc_rules: 'Kimo faqat o\'zi sizga yozgan yoki havola orqali obuna bo\'lgan odamlarga yozadi. Chatga kuniga bittadan ortiq emas. «Stop» so\'zi xabarnomalarni o\'chiradi.', no_broadcasts: 'Xabarnomalar yo\'q. «+» bosing.',
    via_bot: 'bot orqali', new_broadcast: 'Yangi xabarnoma', edit_broadcast: 'Xabarnoma', chat: 'Chat', manual_id: 'Yoki ID ni qo\'lda kiriting', or_manual: 'Boshqa chat (ID)', days: 'Hafta kunlari', mode_text: 'Matn', mode_ai: 'Mavzu bo\'yicha AI', message: 'Xabar', topic: 'Mavzu', topic_placeholder: 'Masalan: ishlar qalay deb so\'ra',
    bc_window: 'Business orqali Telegram faqat mijoz xabaridan keyin 24 soat ichida yozishga ruxsat beradi. Aks holda Kimo tayyor matnni sizga yuboradi. Har kunlik xabarlar uchun odamlarni havola orqali obuna qiling.', need_days: 'Kamida bitta kunni tanlang',
    sub_link: 'Obuna havolasi', sub_link_hint: 'Odam o\'zi «Start» bosadi, shundan keyin Kimo bot nomidan 24 soatlik oynasiz yozadi.', copy: 'Nusxalash', copied: 'Nusxalandi', history: 'Yuborish tarixi',
    d_daily: 'har kuni', d_weekdays: 'ish kunlari', d_weekend: 'dam olish kunlari', wd1: 'du', wd2: 'se', wd3: 'ch', wd4: 'pa', wd5: 'ju', wd6: 'sh', wd7: 'ya',
    access: 'Ruxsatlar', no_grants: 'Berilgan ruxsatlar yo\'q', until: 'gacha', revoke: 'Qaytarib olish', revoked: 'Ruxsat qaytarib olindi', grant_access: 'Ruxsat berish', user_id: 'Telegram ID', days_n: 'Necha kunga', d_short: 'kun', grant: 'Berish', granted: 'Ruxsat berildi', need_id_days: 'ID va kunlar sonini kiriting',
    settings: 'Sozlamalar', accent: 'Urg\'u rangi', custom_color: 'O\'z rangingiz', language: 'Til', timezone: 'Vaqt mintaqasi', max_per_day: 'Chatga kuniga xabarlar', your_access: 'Sizning ruxsatingiz', role_admin: 'Administrator', role_user: 'Foydalanuvchi',
    payments: 'To\'lov', flags_hint: '.env dagi bayroqlar bilan yoqiladi (SUBSCRIPTION_ENABLED, CLICK_ENABLED).', tokens: 'Tokenlar va kalitlar', not_set: 'kiritilmagan', tokens_hint: 'Qiymatlar yashirin va ilovaga berilmaydi.',
    more: 'Yana', m_reminders: 'Yaqin eslatmalar', m_advice: 'Bugungi maslahatlar', m_broadcasts: 'Mijozlarga jadval bo\'yicha xabarlar', m_agents: 'Holat, yoqish, yangi agent', m_access: 'Ruxsat berish va qaytarish', m_settings: 'Rang, til, vaqt mintaqasi', m_assistant: 'Matnli buyruqlar',
    bs_sent: 'Yuborildi', bs_manual_needed: 'Qo\'lda yuborish kerak', bs_skipped: 'O\'tkazib yuborildi', bs_failed: 'Bajarilmadi', bs_stopped: 'To\'xtatildi', bs_blocked: 'Bloklandi', bs_missed: 'Vaqt o\'tib ketdi', bs_retry: 'Qayta urinish',
    open_in_tg: 'Telegramdan oching', open_in_tg_hint: 'Bu ilova faqat Telegram ichida ishlaydi.', no_access: 'Ruxsat berilmagan', no_access_hint: 'Bu ID ni egasiga yuboring, u ruxsat beradi.', retry: 'Qayta urinish',
    err_generic: 'Nimadir xato ketdi', err_network: 'Serverga ulanib bo\'lmadi', no_rights: 'Bu funksiya sizga mavjud emas', never: 'hech qachon', just_now: 'hozirgina', min_ago: '{n} daq oldin', h_ago: '{n} soat oldin', d_ago: '{n} kun oldin',
  };
  const dicts = { ru, uz };
  let cur = 'ru';
  window.I18N = {
    dicts,
    has(key) { return key in ru; },
    setLang(l) { cur = dicts[l] ? l : 'ru'; },
    t(key, vars) {
      let s = (dicts[cur] && dicts[cur][key]) || ru[key] || key;
      if (vars) for (const [k, v] of Object.entries(vars)) s = s.split('{' + k + '}').join(String(v));
      return s;
    },
  };
})();
