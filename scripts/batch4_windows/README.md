# تشغيل الاستيراد (الدفعة 4) على ويندوز

هذه الأداة تشغّل **نفس** `poetry-static/.d1-final/batch4_runner.py` على ويندوز،
دون أي تعديل في منطقه. الحالة (`batch4_state.json`) تبقى المرجع الوحيد،
فالاستئناف من ويندوز أو من لينكس — أو بينهما — آمن.

## لماذا يلزم هذا؟

المشغّل كُتب لينكس، وثلاثة أشياء مرتبطة بالنظام:

| # | المشكلة | الحل في هذه الأداة |
|---|---|---|
| 1 | `import fcntl` (قفل المشغّل) ⇒ `ModuleNotFoundError` | شيم `fcntl` لويندوز عبر `PYTHONPATH` (`win_shim/fcntl.py`) |
| 2 | مسار السجل `/tmp/opencode/...` | `--log` صريح ⇒ `poetry-static/.d1-final/batch4_runner.log` |
| 3 | `npx` ملف `.cmd` ⇒ `CreateProcess` لا ينفّذه | `node <wrangler>/bin/wrangler.js` عبر `BATCH4_WRANGLER_CMD` |

## الاستعمال

```powershell
# 1) ما الناقص على هذا الجهاز؟
python scripts\batch4_windows\batch4_windows.py --data-root C:\poetry-static --check

# 2) تجهيز شجرة البيانات لويندوز (مرة واحدة، قابل للتراجع)
python scripts\batch4_windows\batch4_windows.py --data-root C:\poetry-static --apply-patches

# 3) تشغيل الاستيراد تحت إشراف
python scripts\batch4_windows\batch4_windows.py --data-root C:\poetry-static --supervise

# 4) أين توقّفنا؟
python scripts\batch4_windows\batch4_windows.py --data-root C:\poetry-static --status

# خطة بلا كتابة (آمن)
python scripts\batch4_windows\batch4_windows.py --data-root C:\poetry-static --dry-run
```

أو عبر PowerShell (يناسب التشغيل في الخلفية وجدولة المهام):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\batch4_windows\batch4_supervisor.ps1 -DataRoot C:\poetry-static
```

## الخيارات

| الخيار | الأثر |
|---|---|
| `--data-root` | مسار `poetry-static` (افتراضياً: شقيقة للمستودع) |
| `--normalize-dir` | مسار `poetry-site/scripts` (يجب أن يحوي `normalize_ar.py`) |
| `--wrangler` | أمر wrangler (JSON list أو أمر عادي) — يتجاوز الكشف التلقائي |
| `--max-writes` / `--poll-seconds` | نفس `--max-writes` (85,000) و`--poll-seconds` (600) |
| `--apply-patches` | تعديل شجرة البيانات (مع نسخة `.orig`) |
| `--supervise` / `--run` / `--dry-run` / `--check` / `--status` | أوضاع التشغيل |

## قواعد السلامة

- **لا تُشغّل مشرفاً على لينكس وآخر على ويندوز في الوقت نفسه** — كلاهما
  يرفع نفس ملفات الدفعات إلى نفس جدول D1.
- `poetry-static/.d1-final/wrangler.toml` فيه `database_id` **وهمي**؛ المشغّل
  يشغّل wrangler من `poetry-static` (بدون `wrangler.toml`) فيُحلّ الاسم عن بُعد.
- تحت **WSL** لا تحتاج `--apply-patches` ولا الشيم: `fcntl` و`npx` أصليان هناك.
- التعديلان reversibles: لِرجوعِ الملف من `*.orig` يكفي نسخه فوق الأصلي.

## المتطلبات

- Node.js (للتشغيل: `node` + `wrangler`).
- Python 3.10+.
- مصادقة Cloudflare: `npx wrangler login` (ملف `~/.wrangler/config` خاص بالجهاز).
- مساحة: `.d1-final/` ‏(375M) + `.cache/reconstruction.sqlite` ‏(1.6G).
