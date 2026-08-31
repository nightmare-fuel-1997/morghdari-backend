// Plain-language copy for the v0.4 story UI — farmer-speak, not engineer-speak.
// fa is the primary language (the owner reads Persian); en mirrors it for the toggle.

export const T = {
  // ── header ──
  brand: { fa: 'مُرغداری', en: 'Morghdari' },
  heroKicker: { fa: 'نسخهٔ ۰٫۴ — موتور هشدار', en: 'Version 0.4 — Alert Engine' },
  heroTitle: {
    fa: 'مرغ‌های شما هرگز نمی‌خوابند؛ حالا سیستم شما هم بیدار است.',
    en: 'Your birds never sleep. Now your system doesn’t either.',
  },
  heroSub: {
    fa: 'حسگرهای سالن، دما و آمونیاک را می‌خوانند. اگر خطرناک شود، سیستم قبل از اینکه ضرر ببیند به شما خبر می‌دهد — روی موبایل، روز و شب.',
    en: 'Sensors watch temperature and ammonia in every saloon. When something turns dangerous, the system calls you before the damage is done — day and night.',
  },
  liveBadge: { fa: 'زنده — دادهٔ واقعی', en: 'LIVE — real data' },
  login: { fa: 'ورود', en: 'Sign in' },
  logout: { fa: 'خروج', en: 'Sign out' },
  langBtn: { fa: 'English', en: 'فارسی' },

  // ── live strip (the farm, right now) ──
  stripTitle: { fa: 'مزرعهٔ شما، همین حالا', en: 'Your farm, right now' },
  stripSub: {
    fa: 'این اعداد از حسگرهای واقعیِ این لحظه می‌آید — نه انیمیشن، نه نمونهٔ ساختگی.',
    en: 'These numbers come from the live sensors of this moment — no animation, no canned sample.',
  },
  saloon: { fa: 'سالن', en: 'Saloon' },
  online: { fa: 'آنلاین', en: 'online' },
  stale: { fa: 'بی‌خبر', en: 'no signal' },
  noData: { fa: 'هنوز داده‌ای نرسیده', en: 'no data yet' },
  temp: { fa: 'دما', en: 'Temp' },
  nh3: { fa: 'آمونیاک', en: 'Ammonia' },

  // ── the four promises (what v0.4 does) ──
  promisesTitle: {
    fa: 'نسخهٔ ۰٫۴ چهار قول می‌دهد — و همین‌جا اثبات می‌کند',
    en: 'Version 0.4 makes four promises — and proves each one here',
  },
  p1Title: { fa: '۱. خطر را زود می‌بیند', en: '1. It sees danger early' },
  p1Body: {
    fa: 'حسگرها هر دقیقه می‌خوانند. اگر آمونیاک یا دما از حدِ مجاز بگذرد، سیستم از همان لحظهٔ اول شمارش را شروع می‌کند — چیزی از قلم نمی‌افتد.',
    en: 'Sensors read every minute. The moment ammonia or temperature crosses the safe line, the system starts counting — nothing slips past.',
  },
  p2Title: { fa: '۲. هشدار به‌موقع می‌رسد، نه دیر و نه زود', en: '2. Alerts arrive on time — not early, not late' },
  p2Body: {
    fa: 'نوسانِ کوتاهِ سنسور زنگ خطر به‌پا نمی‌کند؛ اما خطرِ واقعی که ادامه پیدا کند، دقیقاً سرِ وقتِ تعیین‌شده هشدار می‌دهد. یک بار، دقیق، بدون تکرار.',
    en: 'A brief sensor flicker never rings the alarm. But a real, sustained danger fires exactly on schedule — once, precisely, without repeats.',
  },
  p3Title: { fa: '۳. اگر کسی پاسخ ندهد، زنجیره بالا می‌رود', en: '3. If nobody answers, it escalates' },
  p3Body: {
    fa: 'اول مسئول سالن خبر می‌شود. اگر پاسخی ندهد، مهندس مزرعه. باز هم نه؟ مدیر و دامپزشک. پیام گم نمی‌شود؛ فقط بالاتر می‌رود.',
    en: 'First the saloon keeper is paged. No answer? The farm engineer. Still nothing? The manager and the vet. The message is never lost — it just moves up.',
  },
  p4Title: { fa: '۴. سنسورِ خراب را خودش می‌فهمد', en: '4. It knows when a sensor dies' },
  p4Body: {
    fa: 'اگر حسگری خاموش شود، سیستم تفاوتش را با «هوای سالم» می‌داند. بعد از چند دقیقه بی‌خبری، «حسگر قطع است» هشدار می‌دهد — نه سکوتِ خطرناک.',
    en: 'If a sensor goes silent, the system never mistakes silence for “all fine”. After a few minutes it raises “sensor offline” — dangerous silence is not an option.',
  },

  // ── try-it buttons ──
  tryTitle: { fa: 'خودتان امتحان کنید', en: 'Try it yourself' },
  trySub: {
    fa: 'هر دکمه یک اتفاق واقعی در سیستم می‌سازد — نه نمایشی. نتیجه را در همین صفحه ببینید.',
    en: 'Each button makes something real happen in the system — not a rehearsal. Watch the result on this very page.',
  },
  btnSpike: { fa: 'گاز آمونیاک را بالا ببرید', en: 'Release an ammonia spike' },
  btnSpikeNote: {
    fa: 'در چند ثانیه هشدار قرمز «سطح ۱» می‌بینید — همان که مسئول سالن را با موبایل خبر می‌کند.',
    en: 'Within seconds you’ll see a red Tier-1 alert — the one that pages the saloon keeper’s phone.',
  },
  btnStorm: { fa: 'قطعیِ شبکه را شبیه‌سازی کنید', en: 'Simulate a network outage' },
  btnStormNote: {
    fa: 'دستگاه‌ها داده‌ها را نگه می‌دارند و بعد از وصل‌شدن، با زمانِ درست تحویل می‌دهند — هیچ عددی گم نمی‌شود.',
    en: 'Devices buffer their readings and deliver them later with the ORIGINAL timestamps — no number is ever lost.',
  },
  btnSensorOff: { fa: 'یک حسگر را خاموش کنید', en: 'Switch a sensor off' },
  btnSensorOffNote: {
    fa: 'بعد از مهلتِ کوتاه، «حسگر قطع است» ظاهر می‌شود — سیستم سکوت را با سلام اشتباه نمی‌گیرد.',
    en: 'After a short grace period, “sensor offline” appears — the system never mistakes silence for health.',
  },
  btnRecover: { fa: 'همه‌چیز را سالم کن', en: 'Make everything healthy again' },
  btnRecoverNote: {
    fa: 'هشدارها با بهبودِ هوا خودکار بسته می‌شوند و حسگرها دوباره سبز می‌شوند.',
    en: 'Alerts auto-close as the air recovers and sensors turn green again.',
  },
  btnAck: { fa: 'تأیید هشدار', en: 'Acknowledge alert' },
  btnAckNote: {
    fa: 'هشدار بازی را تأیید کنید — ارتقا به سطح بعدی موقتاً متوقف می‌شود.',
    en: 'Acknowledge the open alert — escalation to the next tier pauses.',
  },
  ackBtn: { fa: 'تأیید', en: 'Ack' },
  ackNone: {
    fa: 'هیچ هشدار بازی برای تأیید نیست.',
    en: 'No open alert to acknowledge.',
  },
  busy: { fa: 'در حال انجام…', en: 'working…' },

  // ── alert inbox ──
  inboxTitle: { fa: 'صندوق هشدارها', en: 'Alert inbox' },
  inboxEmpty: { fa: 'هیچ هشداری باز نیست — هوا سالم است.', en: 'No open alerts — the air is healthy.' },
  tier1: { fa: 'سطح ۱ — مسئول سالن', en: 'Tier 1 — saloon keeper' },
  tier2: { fa: 'سطح ۲ — مهندس مزرعه', en: 'Tier 2 — farm engineer' },
  tier3: { fa: 'سطح ۳ — مدیر و دامپزشک', en: 'Tier 3 — manager & vet' },
  resolved: { fa: 'برطرف شد', en: 'resolved' },
  justNow: { fa: 'همین حالا', en: 'just now' },
  minAgo: { fa: 'دقیقه پیش', en: 'min ago' },
  value: { fa: 'مقدار', en: 'value' },
  overLimit: { fa: 'بالای حد مجاز', en: 'over safe limit' },
  skipToContent: { fa: 'پرش به محتوای اصلی', en: 'Skip to main content' },
  mainTitle: { fa: 'پیشخوان مزرعهٔ زنده', en: 'Live farm dashboard' },
  heroTitle2: { fa: 'به مُرغداری خوش آمدید', en: 'Welcome to Morghdari' },

  // ── login ──
  loginTitle: { fa: 'برای دیدن مزرعهٔ زنده وارد شوید', en: 'Sign in to see the live farm' },
  loginHint: {
    fa: 'حساب نمایشی: demo-owner / demo-pass-123 — همان نقش «مالک» که در نسخهٔ نهایی هم وجود دارد.',
    en: 'Demo account: demo-owner / demo-pass-123 — the same “owner” role the real product has.',
  },
  username: { fa: 'نام کاربری', en: 'Username' },
  password: { fa: 'رمز عبور', en: 'Password' },
  loginBtn: { fa: 'ورود', en: 'Sign in' },
  loginError: { fa: 'ورود ناموفق — دوباره تلاش کنید.', en: 'Sign-in failed — try again.' },
  requestFailed: {
    fa: 'درخواست ناموفق بود (کد {code}) — دوباره تلاش کنید.',
    en: 'Request failed (code {code}) — please try again.',
  },

  // ── reliability / quality strip ──
  qualityTitle: { fa: 'قابلیت اطمینان — اعداد واقعی سیستم', en: 'Reliability — real system numbers' },
  qMttr: { fa: 'میانگین رفع هشدار', en: 'Mean time to resolve' },
  qAck: { fa: 'اثر تأیید دستی', en: 'Effect of acknowledgement' },
  qAckAcked: { fa: 'تأییدشده', en: 'acked' },
  qAckNot: { fa: 'تأییدنشده', en: 'not acked' },
  qFlocks: { fa: 'هشدارها در هر دورهٔ پرورش', en: 'Alerts per flock' },
  tier1In: { fa: 'سطح ۱ تا {t} دیگر', en: 'Tier 1 in {t}' },
  nextTierIn: { fa: 'سطح بعدی تا {t} دیگر', en: 'Next tier in {t}' },

  // ── footer ──
  footerTech: {
    fa: 'برای تیم فنی: کنسول مهندسی',
    en: 'For the tech team: engineering console',
  },
  stateTitle: {
    en: 'Alert engine — live state machines',
    fa: 'موتور هشدار — ماشین‌های حالت زنده',
  },
  stateSub: {
    en: 'Live Redis state: breach accumulation, LOS guards, correlation suppression and maintenance silences.',
    fa: 'وضعیت واقعی Redis: انباشت نقض‌ها، نگهبان‌های LOS، سرکوب همبستگی و بی‌صدا کردن نگهداری.',
  },
  stateEmpty: {
    en: 'No active state right now — trigger an alert to see it live.',
    fa: 'در حال حاضر حالتی فعال نیست — برای دیدن زنده، هشداری ایجاد کنید.',
  },
  footerNote: {
    fa: 'نسخهٔ ۰٫۴ · همهٔ اعداد از خطِ واقعیِ داده می‌آیند',
    en: 'v0.4 · every number comes from the real data path',
  },
}
