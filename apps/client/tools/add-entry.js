#!/usr/bin/env node
/**
 * Пополнение архива скриншотов с распознаванием текста.
 *
 * Вы даёте только картинку — текст с неё вытаскивает OCR (tesseract.js,
 * локально, без внешних сервисов) и сохраняет рядом со скриншотом. На странице
 * текст не показывается, но по нему идёт поиск.
 *
 * Интерактивно (основной режим):
 *   node tools/add-entry.js
 *     Спрашивает путь к картинке, распознаёт, сохраняет — и спрашивает снова.
 *     Можно указать ПАПКУ: обработаются все картинки внутри.
 *     Esc — закончить. Незавершённая запись не сохраняется.
 *
 * Разово:
 *   node tools/add-entry.js -i shot.png
 *   node tools/add-entry.js -i ~/screens/          (вся папка)
 *
 * Прочее:
 *   node tools/add-entry.js --list             показать записи
 *   node tools/add-entry.js --remove <id>      удалить запись
 *   node tools/add-entry.js --lang rus+eng     языки OCR (по умолчанию rus+eng)
 *   node tools/add-entry.js --reocr            перераспознать все записи заново
 *
 * Скрипт только правит файлы на диске. Коммит и пуш — вручную.
 */

const fs = require('fs');
const path = require('path');
const readline = require('readline');

const HTML_DIR = path.join(__dirname, '..', 'html');
const AS_DIR = path.join(HTML_DIR, 'as');
const SCREENS_DIR = path.join(AS_DIR, 'screens');
const DATA_FILE = path.join(AS_DIR, 'data.json');

const ALLOWED_EXT = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif']);
const DEFAULT_LANG = 'rus+eng';

class Aborted extends Error {}

// ---------- аргументы ----------

function parseArgs(argv) {
  const out = { flags: new Set(), lang: DEFAULT_LANG };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--list' || a === '--help' || a === '-h' || a === '--reocr') {
      out.flags.add(a.replace(/^-+/, ''));
    } else if (a === '--remove') {
      out.remove = argv[++i];
    } else if (a === '--lang') {
      out.lang = argv[++i];
    } else if (a === '-i' || a === '--image') {
      out.image = argv[++i];
    }
  }
  return out;
}

// ---------- данные ----------

function readData() {
  if (!fs.existsSync(DATA_FILE)) return { entries: [] };
  try {
    const parsed = JSON.parse(fs.readFileSync(DATA_FILE, 'utf8'));
    return Array.isArray(parsed) ? { entries: parsed } : { entries: parsed.entries || [] };
  } catch (err) {
    console.error(`✖ ${DATA_FILE} повреждён: ${err.message}`);
    process.exit(1);
  }
}

function writeData(data) {
  fs.mkdirSync(AS_DIR, { recursive: true });
  fs.writeFileSync(DATA_FILE, JSON.stringify(data, null, 2) + '\n', 'utf8');
}

const TRANSLIT = {
  а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e', ж: 'zh', з: 'z',
  и: 'i', й: 'y', к: 'k', л: 'l', м: 'm', н: 'n', о: 'o', п: 'p', р: 'r',
  с: 's', т: 't', у: 'u', ф: 'f', х: 'h', ц: 'c', ч: 'ch', ш: 'sh', щ: 'sch',
  ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya',
};

function slugify(text) {
  return String(text)
    .toLowerCase()
    .split('')
    .map((ch) => (TRANSLIT[ch] !== undefined ? TRANSLIT[ch] : ch))
    .join('')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40) || 'shot';
}

function uniqueName(base, ext) {
  fs.mkdirSync(SCREENS_DIR, { recursive: true });
  let name = `${base}${ext}`;
  let n = 2;
  while (fs.existsSync(path.join(SCREENS_DIR, name))) {
    name = `${base}-${n++}${ext}`;
  }
  return name;
}

// ---------- OCR ----------

// Воркер поднимается несколько секунд (грузит языковые модели), поэтому
// создаём его один раз на сессию и переиспользуем для всех картинок.
let workerPromise = null;

function getWorker(lang) {
  if (!workerPromise) {
    const { createWorker } = require('tesseract.js');
    console.log(`  (первый запуск: загружаю модели «${lang}», это разово)`);
    workerPromise = createWorker(lang);
  }
  return workerPromise;
}

async function closeWorker() {
  if (!workerPromise) return;
  try {
    const worker = await workerPromise;
    await worker.terminate();
  } catch {
    /* воркер и так мёртв — гасить нечего */
  }
  workerPromise = null;
}

async function recognize(imagePath, lang) {
  const worker = await getWorker(lang);
  const { data } = await worker.recognize(imagePath);
  // Схлопываем пробелы и переводы строк: OCR любит рвать текст, а для поиска
  // важны сами слова, а не вёрстка.
  const text = (data.text || '').replace(/\s+/g, ' ').trim();
  return { text, confidence: Math.round(data.confidence || 0) };
}

// ---------- источник картинок ----------

// Принимает путь к файлу или к папке; возвращает список картинок.
function resolveImages(rawPath) {
  const p = path.resolve(rawPath.replace(/^["']|["']$/g, ''));
  if (!fs.existsSync(p)) return { error: `Не найдено: ${p}` };

  if (fs.statSync(p).isDirectory()) {
    const files = fs.readdirSync(p)
      .filter((f) => ALLOWED_EXT.has(path.extname(f).toLowerCase()))
      .map((f) => path.join(p, f))
      .sort();
    if (!files.length) return { error: `В папке нет картинок: ${p}` };
    return { files };
  }

  const ext = path.extname(p).toLowerCase();
  if (!ALLOWED_EXT.has(ext)) {
    return { error: `Формат "${ext}" не поддерживается. Допустимо: ${[...ALLOWED_EXT].join(', ')}` };
  }
  return { files: [p] };
}

// Распознаёт и сохраняет одну картинку. Копирование в архив происходит ПОСЛЕ
// успешного OCR — при сбое не остаётся осиротевших файлов.
async function ingest(src, data, lang) {
  const label = path.basename(src);
  process.stdout.write(`  ${label} … `);

  let ocr = { text: '', confidence: 0 };
  try {
    ocr = await recognize(src, lang);
  } catch (err) {
    console.log(`OCR не удался (${err.message}) — сохраняю без текста`);
  }

  const base = slugify(ocr.text.slice(0, 60)) || slugify(path.parse(src).name);
  const name = uniqueName(base, path.extname(src).toLowerCase());
  fs.copyFileSync(src, path.join(SCREENS_DIR, name));

  data.entries.push({
    id: Date.now().toString(36) + Math.random().toString(36).slice(2, 5),
    screenshot: `screens/${name}`,
    text: ocr.text,
    confidence: ocr.confidence,
    createdAt: new Date().toISOString(),
  });
  writeData(data);

  const words = ocr.text ? ocr.text.split(' ').length : 0;
  console.log(ocr.text ? `${words} слов, точность ${ocr.confidence}%` : 'текст не распознан');
  return true;
}

// ---------- ввод с поддержкой Esc ----------

function createInput() {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const state = { aborted: false, rejectPending: null, rl };

  if (process.stdin.isTTY) {
    readline.emitKeypressEvents(process.stdin);
    process.stdin.on('keypress', (_str, key) => {
      if (!key) return;
      if (key.name !== 'escape' && !(key.ctrl && key.name === 'c')) return;
      if (state.aborted) return;
      state.aborted = true;
      if (state.rejectPending) state.rejectPending(new Aborted());
      rl.close();
    });
  }
  return state;
}

function ask(state, prompt) {
  return new Promise((resolve, reject) => {
    if (state.aborted) {
      reject(new Aborted());
      return;
    }
    state.rejectPending = reject;
    state.rl.question(prompt, (answer) => {
      state.rejectPending = null;
      resolve(answer.trim());
    });
  });
}

// ---------- команды ----------

function cmdList() {
  const { entries } = readData();
  if (!entries.length) {
    console.log('Архив пуст.');
    return;
  }
  console.log(`Записей: ${entries.length}\n`);
  entries.forEach((e, i) => {
    // Старые записи (вопрос-ответ) и новые (OCR) показываем одинаково удобно.
    const preview = e.text || [e.question, e.answer].filter(Boolean).join(' — ') || '—';
    console.log(`${String(i + 1).padStart(3)}. [${e.id}] ${e.screenshot || 'без картинки'}`);
    console.log(`     ${preview.slice(0, 90)}${preview.length > 90 ? '…' : ''}`);
    if (e.confidence !== undefined) console.log(`     точность OCR: ${e.confidence}%`);
    console.log('');
  });
}

function cmdRemove(id) {
  const data = readData();
  const idx = data.entries.findIndex((e) => e.id === id);
  if (idx === -1) {
    console.error(`✖ Запись "${id}" не найдена. Список: node tools/add-entry.js --list`);
    process.exit(1);
  }
  const [removed] = data.entries.splice(idx, 1);
  if (removed.screenshot) {
    const file = path.join(AS_DIR, removed.screenshot);
    if (fs.existsSync(file)) fs.unlinkSync(file);
  }
  writeData(data);
  console.log(`✔ Удалено: ${removed.screenshot || removed.question || removed.id}`);
}

// Перераспознать уже добавленные картинки — пригодится, если сменили язык
// или добавляли записи до появления OCR.
async function cmdReocr(lang) {
  const data = readData();
  const withImages = data.entries.filter((e) => e.screenshot);
  if (!withImages.length) {
    console.log('Нет записей с картинками.');
    return 0;
  }
  console.log(`Перераспознаю ${withImages.length} картинок (язык: ${lang})\n`);

  let done = 0;
  for (const entry of withImages) {
    const file = path.join(AS_DIR, entry.screenshot);
    if (!fs.existsSync(file)) {
      console.log(`  ${entry.screenshot} … файл отсутствует, пропуск`);
      continue;
    }
    process.stdout.write(`  ${entry.screenshot} … `);
    try {
      const ocr = await recognize(file, lang);
      entry.text = ocr.text;
      entry.confidence = ocr.confidence;
      writeData(data);
      console.log(`${ocr.text.split(' ').length} слов, точность ${ocr.confidence}%`);
      done++;
    } catch (err) {
      console.log(`ошибка: ${err.message}`);
    }
  }
  return done;
}

function cmdAddOnce(args) {
  const resolved = resolveImages(args.image);
  if (resolved.error) {
    console.error(`✖ ${resolved.error}`);
    process.exit(1);
  }
  const data = readData();
  return (async () => {
    let n = 0;
    for (const file of resolved.files) {
      if (await ingest(file, data, args.lang)) n++;
    }
    return n;
  })();
}

async function cmdAddLoop(args) {
  if (!process.stdin.isTTY) {
    console.error('✖ Интерактивный режим требует терминала.');
    console.error('  Для скриптов: node tools/add-entry.js -i shot.png');
    process.exit(1);
  }

  console.log('Добавление скриншотов. Текст с картинки распознаётся автоматически.');
  console.log('Можно указать файл или папку целиком. Esc — закончить.\n');

  const state = createInput();
  const data = readData();
  const startCount = data.entries.length;

  try {
    for (;;) {
      const input = await ask(state, ' Картинка или папка: ');
      if (!input) continue;

      const resolved = resolveImages(input);
      if (resolved.error) {
        console.log(` ⚠ ${resolved.error}`);
        continue;
      }
      if (resolved.files.length > 1) {
        console.log(` Найдено картинок: ${resolved.files.length}`);
      }
      for (const file of resolved.files) {
        await ingest(file, data, args.lang);
      }
      console.log(` Всего в архиве: ${data.entries.length}\n`);
    }
  } catch (err) {
    if (!(err instanceof Aborted)) throw err;
    console.log('\n\nЗавершение.');
  } finally {
    state.rl.close();
    if (process.stdin.isTTY) process.stdin.pause();
  }

  return data.entries.length - startCount;
}

// ---------- точка входа ----------

(async function main() {
  const args = parseArgs(process.argv.slice(2));

  if (args.flags.has('help') || args.flags.has('h')) {
    const doc = fs.readFileSync(__filename, 'utf8').match(/\/\*\*([\s\S]*?)\*\//);
    console.log(doc ? doc[1].replace(/^\s*\* ?/gm, '').trim() : 'см. исходник');
    return;
  }
  if (args.flags.has('list')) {
    cmdList();
    return;
  }
  if (args.remove) {
    cmdRemove(args.remove);
    return;
  }

  let added = 0;
  try {
    if (args.flags.has('reocr')) {
      added = await cmdReocr(args.lang);
      console.log(`\nПерераспознано записей: ${added}`);
    } else if (args.image) {
      added = await cmdAddOnce(args);
      console.log(`\nДобавлено: ${added}`);
    } else {
      added = await cmdAddLoop(args);
      if (added > 0) {
        console.log(`\nДобавлено скриншотов: ${added}. Файлы обновлены на диске.`);
      } else {
        console.log('Ничего не добавлено.');
      }
    }
  } finally {
    await closeWorker();
  }

  if (added > 0) {
    console.log('Выложить в кластер: git add apps/client/html/as && git commit && git push');
  }
})().catch((err) => {
  console.error(`✖ ${err.message}`);
  process.exit(1);
});
