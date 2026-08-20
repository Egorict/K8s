#!/usr/bin/env node
/**
 * Пополнение архива "скрин - вопрос - ответ".
 *
 * Интерактивно (основной режим):
 *   node tools/add-entry.js
 *     Спрашивает скрин, вопрос и ответ, сохраняет запись и сразу начинает
 *     следующую — и так по кругу. Esc завершает работу; запись, начатая но
 *     не доведённая до конца, НЕ сохраняется.
 *
 * Разово, без вопросов:
 *   node tools/add-entry.js -i shot.png -q "Вопрос" -a "Ответ"
 *
 * Прочее:
 *   node tools/add-entry.js --list            показать все записи
 *   node tools/add-entry.js --remove <id>     удалить запись
 *
 * Скрипт только правит файлы на диске. Коммит и пуш — вручную, когда сочтёте
 * нужным.
 */

const fs = require('fs');
const path = require('path');
const readline = require('readline');

const HTML_DIR = path.join(__dirname, '..', 'html');
const AS_DIR = path.join(HTML_DIR, 'as');
const SCREENS_DIR = path.join(AS_DIR, 'screens');
const DATA_FILE = path.join(AS_DIR, 'data.json');

const ALLOWED_EXT = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif']);

// Сигнал "пользователь нажал Esc". Отдельный класс, чтобы отличать отмену
// от настоящей ошибки и не глушить вторую вместе с первой.
class Aborted extends Error {}

// ---------- аргументы ----------

function parseArgs(argv) {
  const out = { flags: new Set() };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--list' || a === '--help' || a === '-h') {
      out.flags.add(a.replace(/^-+/, ''));
    } else if (a === '--remove') {
      out.remove = argv[++i];
    } else if (a === '-i' || a === '--image') {
      out.image = argv[++i];
    } else if (a === '-q' || a === '--question') {
      out.question = argv[++i];
    } else if (a === '-a' || a === '--answer') {
      out.answer = argv[++i];
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

// Кириллица в именах файлов создаёт проблемы при percent-кодировании в URL,
// поэтому имя скриншота транслитерируем из текста вопроса.
function slugify(text) {
  return String(text)
    .toLowerCase()
    .split('')
    .map((ch) => (TRANSLIT[ch] !== undefined ? TRANSLIT[ch] : ch))
    .join('')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40) || 'entry';
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

// ---------- ввод с поддержкой Esc ----------

function createInput() {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const state = { aborted: false, rejectPending: null, rl };

  if (process.stdin.isTTY) {
    readline.emitKeypressEvents(process.stdin);
    process.stdin.on('keypress', (_str, key) => {
      if (!key) return;
      const isEsc = key.name === 'escape';
      const isCtrlC = key.ctrl && key.name === 'c';
      if (!isEsc && !isCtrlC) return;
      if (state.aborted) return;
      state.aborted = true;
      // Прерываем висящий вопрос, чтобы поток управления вышел из цикла.
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

// Ответ может занимать несколько строк: конец — пустая строка.
async function askMultiline(state, prompt) {
  console.log(prompt);
  const lines = [];
  for (;;) {
    const line = await ask(state, lines.length ? '   │ ' : '   │ ');
    if (line === '') {
      if (lines.length) return lines.join('\n');
      continue; // пустой ввод в самом начале — просто ждём дальше
    }
    lines.push(line);
  }
}

// ---------- сбор одной записи ----------

// Возвращает готовый объект. Ничего не пишет на диск: копирование картинки и
// сохранение происходят ПОСЛЕ того, как все три поля собраны. Так отмена на
// любом шаге не оставляет ни осиротевших файлов, ни половинчатых записей.
async function collectEntry(state, index) {
  console.log(`\n${'─'.repeat(52)}`);
  console.log(`Запись #${index}`);

  const imagePath = await ask(state, ' Скриншот (путь, Enter — без картинки): ');
  const question = await ask(state, ' Вопрос: ');
  if (!question) {
    console.log(' ⚠ Вопрос пустой — запись пропущена (по нему идёт поиск).');
    return null;
  }
  const answer = await askMultiline(state, ' Ответ (пустая строка — закончить):');
  if (!answer) {
    console.log(' ⚠ Ответ пустой — запись пропущена.');
    return null;
  }

  let screenshot = null;
  if (imagePath) {
    // Кавычки появляются, если файл перетащили в терминал.
    const src = path.resolve(imagePath.replace(/^["']|["']$/g, ''));
    if (!fs.existsSync(src)) {
      console.log(` ⚠ Файл не найден: ${src} — запись пропущена.`);
      return null;
    }
    const ext = path.extname(src).toLowerCase();
    if (!ALLOWED_EXT.has(ext)) {
      console.log(` ⚠ Формат "${ext}" не поддерживается — запись пропущена.`);
      return null;
    }
    const name = uniqueName(slugify(question), ext);
    fs.copyFileSync(src, path.join(SCREENS_DIR, name));
    screenshot = `screens/${name}`;
  }

  return {
    id: Date.now().toString(36),
    question,
    answer,
    screenshot,
    createdAt: new Date().toISOString(),
  };
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
    console.log(`${String(i + 1).padStart(3)}. [${e.id}] ${e.question}`);
    console.log(`     ответ: ${e.answer.split('\n')[0].slice(0, 70)}`);
    console.log(`     скрин: ${e.screenshot || '—'}\n`);
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
  console.log(`✔ Удалено: ${removed.question}`);
}

// Разовое добавление флагами — без цикла и без интерактива.
function cmdAddOnce(args) {
  let screenshot = null;
  if (args.image) {
    const src = path.resolve(args.image.replace(/^["']|["']$/g, ''));
    if (!fs.existsSync(src)) {
      console.error(`✖ Файл не найден: ${src}`);
      process.exit(1);
    }
    const ext = path.extname(src).toLowerCase();
    if (!ALLOWED_EXT.has(ext)) {
      console.error(`✖ Формат "${ext}" не поддерживается. Допустимо: ${[...ALLOWED_EXT].join(', ')}`);
      process.exit(1);
    }
    const name = uniqueName(slugify(args.question), ext);
    fs.copyFileSync(src, path.join(SCREENS_DIR, name));
    screenshot = `screens/${name}`;
  }

  const data = readData();
  data.entries.push({
    id: Date.now().toString(36),
    question: args.question,
    answer: args.answer,
    screenshot,
    createdAt: new Date().toISOString(),
  });
  writeData(data);
  console.log(`✔ Добавлено. Всего записей: ${data.entries.length}`);
  return 1;
}

async function cmdAddLoop(args) {
  if (!process.stdin.isTTY) {
    console.error('✖ Интерактивный режим требует терминала.');
    console.error('  Для скриптов: node tools/add-entry.js -i shot.png -q "Вопрос" -a "Ответ"');
    process.exit(1);
  }

  console.log('Добавление записей в архив.');
  console.log('Заполняйте по кругу; Esc — закончить и запушить.');
  console.log('Незавершённая запись при выходе не сохраняется.');

  const state = createInput();
  const data = readData();
  const startCount = data.entries.length;
  let index = startCount + 1;

  try {
    for (;;) {
      const entry = await collectEntry(state, index);
      if (entry) {
        data.entries.push(entry);
        writeData(data); // сохраняем сразу — прерывание не потеряет прошлые записи
        console.log(` ✔ Сохранено (всего: ${data.entries.length})`);
        index++;
      }
    }
  } catch (err) {
    if (!(err instanceof Aborted)) throw err;
    console.log('\n\nЗавершение — незаконченная запись отброшена.');
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

  const oneShot = Boolean(args.question && args.answer);
  const added = oneShot ? cmdAddOnce(args) : await cmdAddLoop(args);

  if (added > 0) {
    const word = added === 1 ? 'запись' : 'записей';
    console.log(`\nДобавлено ${added} ${word}. Файлы обновлены на диске.`);
    console.log('Выложить в кластер: git add apps/client/html/as && git commit && git push');
  } else {
    console.log('Ничего не добавлено.');
  }
})().catch((err) => {
  console.error(`✖ ${err.message}`);
  process.exit(1);
});
