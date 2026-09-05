"""Узбекский (латиница) — эталон формулировок.

Ключ, которого здесь нет, не существует: `smoke_block5.py` сверяет каждый
вызов `t()` с этим словарём. Русский может отставать, узбекский — нет.

Порядок разделов повторяет порядок экранов: меню, вход, профиль, поручения,
встречи, решения, документы, администрирование, общие ответы. Ключ читается
как путь: `menu.tasks`, `task.status.done`, `error.no_rights`.

Кириллица отсюда выводится правилом и руками не набирается.
"""

TABLE: dict[str, str] = {
    # ── Меню ────────────────────────────────────────────────────────────────
    "menu.my_day": "📅 Mening kunim",
    "menu.my_meetings": "📅 Uchrashuvlarim",
    "menu.my_tasks": "📋 Topshiriqlarim",
    "menu.request_meeting": "➕ Uchrashuv soʻrash",
    "menu.new_task": "➕ Topshiriq",
    "menu.quick_meeting": "⚡ Yigʻilish",
    "menu.decisions": "📌 Qarorlar",
    "menu.search": "🔎 Qidiruv",
    "menu.control": "📊 Nazorat",
    "menu.availability": "🟢 Bandligim",
    "menu.who_is_open": "👤 Kim aloqada",
    "menu.admin": "🛠 Boshqaruv",
    "menu.profile": "👤 Profil",
    "menu.help": "❓ Yordam",

    # ── Роли ────────────────────────────────────────────────────────────────
    "role.executive": "Rahbar",
    "role.assistant": "Yordamchi",
    "role.dept_head": "Boʻlim boshligʻi",
    "role.employee": "Xodim",
    "role.admin": "Administrator",
    "role.auditor": "Auditor",
    "role.none": "rolsiz",

    # ── Статусы поручения ───────────────────────────────────────────────────
    "task.status.new": "Yangi",
    "task.status.acknowledged": "Qabul qilingan",
    "task.status.in_progress": "Bajarilmoqda",
    "task.status.review": "Tekshiruvda",
    "task.status.done": "Bajarildi",
    "task.status.blocked": "Toʻxtatilgan",
    "task.status.overdue": "Muddati oʻtgan",
    "task.status.cancelled": "Bekor qilingan",

    # ── Приоритеты ──────────────────────────────────────────────────────────
    "priority.low": "Past",
    "priority.normal": "Oddiy",
    "priority.high": "Yuqori",
    "priority.critical": "Juda muhim",

    # ── Доступность ─────────────────────────────────────────────────────────
    "availability.open": "🟢 Qabul qilaman",
    "availability.busy": "🟡 Bandman",
    "availability.dnd": "🔴 Bezovta qilmang",
    "availability.offline": "⚪️ Belgilanmagan",
    "availability.late": "🌙 Kechki qabul",

    # ── Дни недели ──────────────────────────────────────────────────────────
    "weekday.0": "dushanba",
    "weekday.1": "seshanba",
    "weekday.2": "chorshanba",
    "weekday.3": "payshanba",
    "weekday.4": "juma",
    "weekday.5": "shanba",
    "weekday.6": "yakshanba",
    "weekday.short.0": "Du",
    "weekday.short.1": "Se",
    "weekday.short.2": "Ch",
    "weekday.short.3": "Pa",
    "weekday.short.4": "Ju",
    "weekday.short.5": "Sh",
    "weekday.short.6": "Ya",

    # ── Месяцы ──────────────────────────────────────────────────────────────
    "month.1": "yanvar",
    "month.2": "fevral",
    "month.3": "mart",
    "month.4": "aprel",
    "month.5": "may",
    "month.6": "iyun",
    "month.7": "iyul",
    "month.8": "avgust",
    "month.9": "sentabr",
    "month.10": "oktabr",
    "month.11": "noyabr",
    "month.12": "dekabr",

    # ── Даты в ответах ──────────────────────────────────────────────────────
    "date.today": "Bugun",
    "date.tomorrow": "Ertaga",
    "date.day_after_tomorrow": "Indinga",
    "date.yesterday": "Kecha",
    "date.on.0": "Dushanba kuni",
    "date.on.1": "Seshanba kuni",
    "date.on.2": "Chorshanba kuni",
    "date.on.3": "Payshanba kuni",
    "date.on.4": "Juma kuni",
    "date.on.5": "Shanba kuni",
    "date.on.6": "Yakshanba kuni",

    # ── Вход и регистрация ──────────────────────────────────────────────────
    "start.greeting": "Assalomu alaykum, {name}!",
    "start.ask_name": "Familiya, ism va otangizning ismini yozing.",
    "start.ask_role": "Qaysi rolda ishlaysiz?",
    "start.ask_contact": "Raqamingizni tasdiqlang — pastdagi tugmani bosing.",
    "start.contact_button": "📱 Raqamni tasdiqlash",
    "start.pending": "Arizangiz administrator koʻrigida. Tasdiqlanishi bilan xabar beramiz.",
    "start.approved": "Arizangiz tasdiqlandi. Tizimdan foydalanishingiz mumkin.",
    "start.rejected": "Arizangiz rad etildi. Administratorga murojaat qiling.",
    "start.skip": "Oʻtkazib yuborish",
    "start.need_registration": "Tizimdan foydalanish uchun /start bosing va roʻyxatdan oʻting.",
    "start.suspended": "Kirish toʻxtatilgan. Administratorga murojaat qiling.",
    "start.no_access": "Tizimga kirish ochilmagan. Administratorga murojaat qiling.",
    "start.your_id": "Sizning Telegram raqamingiz: <code>{id}</code>",
    "start.id_hint": "U tizimning birinchi administratorini tayinlash uchun kerak.",
    "start.hello": "Assalomu alaykum! Bu uchrashuvlar va topshiriqlar boʻyicha korporativ yordamchi.",
    "start.invite_label": "Taklif: <b>{label}</b>",
    "start.invite_bad": "Taklif havolasi yaroqsiz yoki allaqachon ishlatilgan.\nAdministratordan yangisini soʻrang.",
    "start.already_pending": "Arizangiz administratorga yuborilgan.\nTasdiqlanishi bilan bot xabar yuboradi.",
    "start.closed": "Tizimga kirish yopiq. Administratorga murojaat qiling.",
    "start.name_too_short": "Ism juda qisqa. Familiya va ismni toʻliq yozing.",
    "start.ask_department": "Qaysi boʻlimda ishlaysiz?",
    "start.role_needs_approval": "«{role}» rolini administrator tasdiqlaydi — bu koʻp vaqt olmaydi.",
    "start.welcome_back": "Xush kelibsiz, <b>{name}</b>.",
    "start.role_line": "Rol: {roles}",
    "start.availability_line": "Bandlik: {state}",
    "start.done": "Tayyor, {name}. Siz tizimdasiz.",
    "start.first_admin": "Siz tizimdagi birinchi odamsiz, shuning uchun sizga <b>administrator</b> roli berildi — arizalarni tasdiqlaydigan boshqa odam yoʻq edi.\n\nNimadan boshlash kerak:\n1. «🛠 Boshqaruv» → «Boʻlimlar» — boʻlimlarni oching\n2. «Taklif havolalari» — havolani boʻlim chatiga yuboring\n3. Rahbar roʻyxatdan oʻtsin, arizasini tasdiqlang",
    "start.request_sent": "Ariza administratorga yuborildi.\nTasdiqlanishi bilan bot xabar yuboradi — qayta yozish shart emas.",
    "start.own_number": "Iltimos, oʻz raqamingizni yuboring — pastdagi tugma bilan.",
    "start.press_contact": "«📱 Raqamni tasdiqlash» tugmasini bosing — raqamni qoʻlda yozish shart emas.",
    "start.new_application": "📥 <b>Yangi roʻyxatdan oʻtish arizasi</b>",
    "start.requested_role": "Soʻralgan rol: <b>{role}</b>",

    # ── Профиль ─────────────────────────────────────────────────────────────
    "profile.title": "Profil",
    "profile.department": "Boʻlim",
    "profile.department_none": "koʻrsatilmagan",
    "profile.roles": "Rol",
    "profile.phone": "Telefon",
    "profile.phone_none": "raqam tasdiqlanmagan",
    "profile.workday": "Ish kuni",
    "profile.workday_none": "belgilanmagan",
    "profile.timezone": "Vaqt mintaqasi",
    "profile.availability": "Bandlik",
    "profile.availability_until": "amal qiladi: {time} gacha",
    "profile.language": "Til",
    "profile.change_language": "🌐 Tilni oʻzgartirish",
    "profile.choose_language": "Qaysi tilda gaplashamiz?",
    "profile.language_changed": "Til oʻzgartirildi: {language}",

    # ── Помощь ──────────────────────────────────────────────────────────────
    "help.title": "Qanday foydalaniladi",
    "help.intro": "Tizim uchrashuvlar, topshiriqlar va ularning bajarilishini yuritadi.",
    "help.works_now": (
        "Hozir ishlaydi: roʻyxatdan oʻtish va huquqlar, muddatli topshiriqlar "
        "va nazorat, eslatmalar, bandlik belgisi, kalendar va uchrashuvlar, "
        "qarorlar reyestri, matni boʻyicha qidiriladigan hujjatlar, "
        "Excel va PDF ga yuklash."
    ),
    "help.documents_title": "Hujjatlar",
    "help.documents": (
        "Faylni shunchaki botga yuboring — u kimga ochishni soʻraydi. "
        "Odatda hujjat faqat sizga koʻrinadi: uchrashuvga kirish uning "
        "hujjatlariga kirish demak emas."
    ),
    "help.search_title": "Qidiruv",
    "help.search": (
        "«Qidiruv» tugmasi uchrashuvlar, topshiriqlar, qarorlar, odamlar va "
        "hujjatlar matni boʻyicha birdan qidiradi. Faqat sizga ochilgani topiladi."
    ),
    "help.availability_title": "Bandlik belgisi",
    "help.availability": (
        "«Bandligim» tugmasi xodimlarga hozir qabul qilayotganingizni bildiradi. "
        "Holatning har doim muddati bor: muddat tugagach belgi oʻzi olinadi."
    ),
    "help.commands": (
        "Buyruqlar: /start — bosh menyu, /help — shu yordam, "
        "/id — Telegram raqamingiz."
    ),

    # ── Поручения ───────────────────────────────────────────────────────────
    "task.new.title": "Yangi topshiriq",
    "task.new.ask_assignee": "Kimga topshiramiz?",
    "task.new.ask_title": "Nima qilish kerak? Bir qatorda yozing.",
    "task.new.ask_due": (
        "Qaysi muddatga?\n\nSoʻz bilan ham yozish mumkin: <i>ertaga</i>, "
        "<i>juma gacha</i>, <i>3 kundan keyin</i>, <i>05.09</i>"
    ),
    "task.new.ask_priority": "Muhimligi qanday?",
    "task.new.no_due": "Muddatsiz",
    "task.new.created": "Topshiriq yaratildi",
    "task.new.too_short": "Juda qisqa. Vazifani tushunarli yozing.",
    "task.new.nobody": (
        "Hozircha topshiradigan odam yoʻq: mas'uliyat doirangizda faol xodim yoʻq.\n"
        "«Boshqaruv» boʻlimida boʻlimlar oching va taklif havolalarini tarqating."
    ),
    "task.list.active": "Faol",
    "task.list.today": "Bugunga",
    "task.list.overdue": "Muddati oʻtgan",
    "task.list.review": "Tekshiruvda",
    "task.list.created": "Boshqalarga topshirganlarim",
    "task.list.done": "Bajarilgan",
    "task.list.empty": "Boʻsh.",
    "task.action.accept": "✅ Qabul qilish",
    "task.action.start": "▶️ Ishga olish",
    "task.action.submit": "📤 Bajarildi",
    "task.action.approve": "✅ Ishni qabul qilish",
    "task.action.reject": "↩️ Qayta ishlashga",
    "task.action.extend": "⏰ Muddatni uzaytirish",
    "task.action.extend_ok": "⏰ Uzaytirish",
    "task.action.extend_no": "✖️ Rad etish",
    "task.action.comment": "💬 Izoh",
    "task.action.cancel": "⚫ Bekor qilish",
    "task.action.save_template": "📑 Namuna sifatida saqlash",
    "task.field.assignee": "Ijrochi",
    "task.field.author": "Muallif",
    "task.field.due": "Muddat",
    "task.field.priority": "Muhimligi",
    "task.field.status": "Holati",
    "task.field.no_due": "muddatsiz",
    "task.comments.title": "Soʻnggi izohlar",
    "task.last_change": "Soʻnggi oʻzgarish",

    # ── Шаблоны ─────────────────────────────────────────────────────────────
    "template.title": "Namunaviy topshiriqlar",
    "template.subtitle": "Bir bosish — va topshiriq tayyor.",
    "template.all": "📚 Barcha namunalar",
    "template.saved": "Namuna tayyor",
    "template.saved_hint": "U «Topshiriq» ekranida koʻrinadi.",
    "template.due_hint": "Muddat: qoʻllangan kundan {days} kun",
    "template.exists": "Bunday namuna allaqachon bor.",
    "template.duplicate_task": "Bu topshiriq allaqachon yaratilgan va hali qabul qilinmagan.",
    "template.deleted": "🗑 «{title}» namunasi oʻchirildi.",
    "template.ask_assignee": "Kimga topshiramiz?",
    "template.empty": "Namunalar hali yoʻq.",

    # ── Встречи ─────────────────────────────────────────────────────────────
    "meeting.day.title": "Mening kunim · {date}",
    "meeting.day.now": "Hozir",
    "meeting.day.next": "Keyin",
    "meeting.day.none": "Bugunga uchrashuv yoʻq.",
    "meeting.day.free_from": "🟢 {time} dan boʻsh",
    "meeting.day.needs_decision": "Qaror talab qiladi",
    "meeting.day.requests": "📥 Uchrashuv soʻrovlari: {count}",
    "meeting.day.requests_over": ", limitdan ortiq {count}",
    "meeting.day.to_review": "🔍 Tekshiruvingizni kutmoqda: {count}",
    "meeting.day.stale_decisions": "📌 Muddati oʻtgan qarorlar: {count}",
    "meeting.day.overdue": "Muddati oʻtdi: {count}",
    "meeting.day.overdue_other": "• qolgan boʻlimlarda: {count}",
    "meeting.day.personal": "Shaxsiy nazoratda",
    "meeting.day.metrics": "30 kunlik koʻrsatkichlar",
    "meeting.day.quiet": "Diqqat talab qiladigan narsa yoʻq.",
    "meeting.mine.title": "Uchrashuvlarim",
    "meeting.mine.empty": (
        "Yaqin ikki haftada uchrashuv yoʻq.\n"
        "Kelishish uchun «Uchrashuv soʻrash» tugmasini bosing."
    ),
    "meeting.card.host": "Boshlovchi",
    "meeting.card.participants": "Ishtirokchilar",
    "meeting.card.cancelled": "🚫 Bekor qilindi. Sabab: {reason}",
    "meeting.card.no_reason": "koʻrsatilmagan",
    "meeting.card.moved": "🔄 {count} marta koʻchirildi",
    "meeting.card.finished": "🏁 Yakunlandi. Qaror va topshiriq qayd etilmadi.",
    "meeting.card.outcome": "🏁 Natija: qaror {decisions}, topshiriq {tasks}",
    "meeting.action.here": "🙋 Men joydaman",
    "meeting.action.move": "🔄 Koʻchirish",
    "meeting.action.cancel": "🚫 Bekor qilish",
    "meeting.action.decision": "📌 Qaror",
    "meeting.action.task": "➕ Topshiriq",
    "meeting.action.files": "📎 Hujjatlar",
    "meeting.action.agenda": "📋 Kun tartibi",
    "meeting.action.finish": "🏁 Uchrashuvni yakunlash",
    "meeting.checkin.ok": "Belgiladim, rahmat.",
    "meeting.checkin.not_participant": "Siz bu uchrashuv ishtirokchilari roʻyxatida yoʻqsiz.",
    "meeting.checkin.early": "Boshlanishiga besh daqiqa qolganda belgilash mumkin.",
    "meeting.checkin.late": "Uchrashuv tugadi — endi belgini yordamchi qoʻyadi.",
    "meeting.request.busy": "Bu vaqt band.",
    "meeting.request.alternatives": "Boshqa vaqt boʻsh:",
    "meeting.request.taken": "Bu vaqtni hozirgina band qilishdi.",
    "meeting.request.past": "Bu vaqt oʻtib ketgan.",
    "meeting.request.sent": "Soʻrov yuborildi, vaqt siz uchun ushlab turildi.",
    "meeting.request.declined": "❌ Uchrashuv rad etildi",
    "meeting.request.approved": "✅ Uchrashuv tasdiqlandi",
    "meeting.request.expired": "⌛️ Javobsiz soʻrov",
    "meeting.agenda.title": "Kun tartibi",
    "meeting.agenda.empty": "Hozircha band yoʻq.",

    # ── Решения ─────────────────────────────────────────────────────────────
    "decision.registry": "Qarorlar reyestri",
    "decision.open": "Yopilmagan qarorlar",
    "decision.none": "Yopilmagan qaror yoʻq.",
    "decision.new": "➕ Qaror",
    "decision.ask_title": "Qarorni bir qatorda ifodalang.",
    "decision.saved": "📌 Yozildi",
    "decision.done": "✅ Bajarildi",
    "decision.cancel": "🚫 Bekor qilish",
    "decision.ask_reason": "Nega bekor qilamiz? Sabab reyestrda abadiy qoladi.",
    "decision.closed": "✅ Qaror bajarilgan deb belgilandi.",
    "decision.cancelled": "🚫 Qaror bekor qilindi, sabab reyestrga yozildi.",
    "decision.status.open": "Ishda",
    "decision.status.done": "Bajarildi",
    "decision.status.cancelled": "Bekor qilindi",
    "decision.responsible": "Mas'ul",
    "decision.author": "Muallif",
    "decision.not_open": "Bu qaror sizga ochilmagan.",

    # ── Документы ───────────────────────────────────────────────────────────
    "document.received": "Hujjat qabul qilindi",
    "document.ask_scope": "Kimga ochamiz?",
    "document.scope.private": "Faqat menga",
    "document.scope.participants": "Uchrashuv ishtirokchilariga",
    "document.scope.department": "Boʻlimga",
    "document.scope.organization": "Hammaga",
    "document.not_open": "Bu hujjat sizga ochilmagan.",
    "document.meeting_files": "Uchrashuv hujjatlari",
    "document.meeting_none": (
        "Bu uchrashuvning sizga ochilgan hujjatlari yoʻq.\n"
        "Oʻzingiznikini biriktirish uchun faylni botga yuboring."
    ),
    "document.views": "👁 Kim ochgan",
    "document.never_opened": "Hujjatni hali hech kim ochmagan.",
    "document.share": "🔓 Kirish ochildi",

    # ── Поиск ───────────────────────────────────────────────────────────────
    "search.ask": "Nimani qidiramiz? Soʻz yoki familiya yozing.",
    "search.too_short": "Kamida ikkita belgi yozing.",
    "search.nothing": "Hech narsa topilmadi.",
    "search.meetings": "Uchrashuvlar",
    "search.tasks": "Topshiriqlar",
    "search.decisions": "Qarorlar",
    "search.documents": "Hujjatlar",
    "search.people": "Xodimlar",

    # ── Администрирование ───────────────────────────────────────────────────
    "admin.title": "Boshqaruv",
    "admin.pending": "📥 Roʻyxatdan oʻtish arizalari",
    "admin.users": "👥 Xodimlar",
    "admin.departments": "🏢 Boʻlimlar",
    "admin.invites": "🔗 Taklif havolalari",
    "admin.hours": "🕐 Ish vaqti",
    "admin.quotas": "⏳ Vaqt limitlari",
    "admin.holidays": "📆 Bayramlar",
    "admin.absences": "🏖 Ta'tillar",
    "admin.features": "🎛 Tizim boʻlimlari",
    "admin.audit": "📜 Harakatlar jurnali",
    "admin.back": "⬅️ Boshqaruvga",
    "admin.no_rights": "Huquq yetarli emas.",
    "admin.features.title": "Tizim boʻlimlari",
    "admin.features.hint": (
        "Oʻchirilgan boʻlim menyudan yoʻqoladi va ochilmay qoʻyadi. "
        "Ma'lumotlar joyida qoladi: qayta yoqsangiz — hammasi oʻrnida."
    ),
    "admin.features.done": "Tayyor. Menyu keyingi /start da yangilanadi.",
    "admin.hours.title": "Ish vaqti · {name}",
    "admin.hours.whose": "Kimning jadvalini oʻzgartiramiz?",
    "admin.hours.dayoff": "dam olish",
    "admin.hours.lunch": "tushlik {start}–{end}",
    "admin.hours.buffer": "Uchrashuvlar orasidagi tanaffus: {minutes} daqiqa",
    "admin.hours.in_a_row": "Ketma-ket uchrashuv soni: {count} tadan koʻp emas",
    "admin.hours.howto": "Oʻzgartirish uchun bir qatorda yuboring:",
    "admin.hours.changed": "🕐 Jadval oʻzgardi: {name}.",
    "admin.hours.applies": "Boʻsh vaqtlar darhol qayta hisoblanadi.",
    "admin.hours.unclear": "Tushunmadim. Namunalar: <code>09:00-18:00</code>, <code>tushlik 13:00-14:00</code>.",
    "admin.holidays.title": "Bayramlar va koʻchirilgan kunlar",
    "admin.holidays.empty": "Hech narsa kiritilmagan.",
    "admin.holidays.working": "🟢 ish kuni",
    "admin.holidays.dayoff": "🔴 dam olish",
    "admin.holidays.added": "📆 {date} — {title}",
    "admin.holidays.removed": "🗑 {date} endi belgilanmagan.",
    "admin.absences.title": "Ta'tillar va xizmat safarlari",
    "admin.absences.empty": "Yaqin kunlarda yoʻqlik yoʻq.",
    "admin.absences.added": "🏖 {name}: {kind} {from_date}–{to_date}.",
    "admin.absences.applies": "Bu kunlar boʻsh vaqtlardan yoʻqoladi.",
    "admin.absences.removed": "🗑 Yoʻqlik olib tashlandi, kunlar kalendarga qaytdi.",
    "admin.quotas.title": "Vaqt limitlari",
    "admin.quotas.none": "Limit belgilanmagan: rahbar vaqti cheklanmagan.",
    "admin.quotas.whose": "Kimning vaqtini cheklaymiz?",
    "admin.quotas.which_department": "Qaysi boʻlimga?",
    "admin.quotas.set": "⏳ «{department}» boʻlimiga — haftasiga {minutes} daqiqa.",
    "admin.quotas.note": "Limitdan oshish soʻrovni taqiqlamaydi, faqat belgilaydi.",
    "admin.audit.title": "Soʻnggi harakatlar",
    "admin.audit.empty": "Jurnal hozircha boʻsh.",

    # ── Виды отсутствия ─────────────────────────────────────────────────────
    "absence.vacation": "ta'til",
    "absence.trip": "xizmat safari",
    "absence.sick": "kasallik varaqasi",
    "absence.other": "yoʻqlik",

    # ── Переключатели разделов ──────────────────────────────────────────────
    "feature.meetings": "Uchrashuvlar va kalendar",
    "feature.documents": "Hujjatlar va ular boʻyicha qidiruv",
    "feature.templates": "Namunaviy topshiriqlar",
    "feature.analytics": "Rahbar ekranidagi koʻrsatkichlar",
    "feature.digest": "Ertalabki xulosa",
    "feature.off": "Bu boʻlim tashkilot administratori tomonidan oʻchirilgan.",

    # ── Показатели ──────────────────────────────────────────────────────────
    "metric.calendar_load": "Kalendar bandligi",
    "metric.time_spenders": "Kim vaqtni sarflaydi",
    "metric.meeting_cost": "Yigʻilishlar narxi",
    "metric.punctuality": "Vaqtga rioya",
    "metric.deadline_discipline": "Muddat intizomi",
    "metric.chronic_extensions": "Doimiy koʻchirishlar",
    "metric.reaction_time": "Javob tezligi",
    "metric.rework_rate": "Qayta ishlashga qaytarish",
    "metric.fruitless_meetings": "Natijasiz uchrashuvlar",
    "metric.decision_speed": "Qaror tezligi",
    "metric.repeating_topics": "Takrorlanuvchi mavzular",
    "metric.availability_lag": "Vaqt kutish",
    "metric.schedule_jams": "Jadval tiqilinchi",
    "metric.overdue_trend": "Muddat buzilishi tendensiyasi",
    "metric.overload_forecast": "Yaqin hafta",
    "metric.no_data": "ma'lumot yoʻq",

    # ── Утренняя сводка ─────────────────────────────────────────────────────
    "digest.title": "☀️ Ertalab · {date}",
    "digest.chiefs_day": "Rahbarda bugun",
    "digest.chief_meetings": "uchrashuv {count}",
    "digest.chief_requests": "javob kutayotgan soʻrov {count}",

    # ── Общие ответы ────────────────────────────────────────────────────────
    "common.yes": "Ha",
    "common.no": "Yoʻq",
    "common.back": "⬅️ Orqaga",
    "common.cancel": "Bekor qilish",
    "common.done": "Tayyor.",
    "common.saved": "Saqlandi.",
    "common.deleted": "Oʻchirildi.",
    "common.open": "Ochish",
    "common.minutes": "daqiqa",
    "common.hours": "soat",
    "common.days": "kun",
    "common.of": "dan",
    "common.today": "bugun",
    "common.tomorrow": "ertaga",
    "common.yesterday": "kecha",

    # ── Отказы и ошибки ─────────────────────────────────────────────────────
    "error.stale_button": "Bu tugma eskirgan. Boʻlimni qaytadan oching.",
    "error.no_rights": "Huquqingiz yetarli emas.",
    "error.not_found": "Topilmadi.",
    "error.other_org": "Bu boshqa tashkilotning yozuvi.",
    "error.failed": "Boʻlmadi.",
    "error.try_again": "Qaytadan urinib koʻring.",
    "error.section_closed": "Boʻlim ochiq emas.",
}
