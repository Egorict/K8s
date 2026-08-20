#!/usr/bin/env node
/**
 * Добавление записи в архив "скрин - вопрос - ответ".
 *
 * Использование:
 *   node tools/add-entry.js                                   # интерактивно
 *   node tools/add-entry.js -i shot.png -q "Вопрос" -a "Ответ"
 *   node tools/add-entry.js --list                            # показать все записи
 *   node tools/add-entry.js --remove <id>                     # удалить запись
 *
 * Скрипт копирует картинку в html/as/screens/ и дописывает запись в
 * html/as/data.json. Дальше остаётся закоммитить и запушить: CI пересоберёт
 * образ клиента, ArgoCD раскатает.
 */

const fs = require('fs');
const path = require('path');
const readline = require('readline');

const HTML_DIR = path.join(__dirname, '..', 'html');
const AS_DIR = path.join(HTML_DIR, 'as');
const SCREENS_DIR = path.join(AS_DIR, 'screens');
const DATA_FILE = path.join(AS_DIR, 'data.json');

const ALLOWED_EXT = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif']);

// ---------- разбор аргументов ----------

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

// ---------- работа с данными ----------

function readData() {
  if (!fs.existsSync(DATA_FILE)) return { entries: [] };
  try {
    const parsed = JSON.parse(fs.readFileSync(DATA_FILE, 'utf8'));
    // Поддерживаем и голый массив, и объект — на случай ручной правки файла.
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

// Транслитерация нужна, чтобы имя файла оставалось читаемым и безопасным
// для URL: кириллица в путях создаёт проблемы при percent-кодировании.
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

// ---------- интерактивный ввод ----------

function ask(rl, question) {
  return new Promise((resolve) => rl.question(question, (ans) => resolve(ans.trim())));
}

// Ответ может быть многострочным, поэтому читаем до пустой строки.
function askMultiline(rl, prompt) {
  return new Promise((resolve) => {
    console.log(prompt);
    const lines = [];
    const onLine = (line) => {
      if (line.trim() === '' && lines.length) {
        rl.removeListener('line', onLine);
        resolve(lines.join('\n').trim());
        return;
      }
      if (line.trim() !== '') lines.push(line);
    };
    rl.on('line', onLine);
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
    console.log(`${String(i + 1).padStart(3)}. [${e.id}] ${e.question}`);
    console.log(`     ответ: ${e.answer.split('\n')[0].slice(0, 70)}`);
    console.log(`     скрин: ${e.screenshot || '—'}\n`);
  });
}

function cmdRemove(id) {
  const data = readData();
  const idx = data.entries.findIndex((e) => e.id === id);
  if (idx === -1) {
    console.error(`✖ Запись с id "${id}" не найдена. Список: node tools/add-entry.js --list`);
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

async function cmdAdd(args) {
  // Если вопрос и ответ пришли флагами — работаем молча, ничего не спрашивая.
  // Так скрипт годится и для интерактива, и для пакетного добавления из другого
  // скрипта, где stdin не терминал и любой prompt намертво завис бы.
  const interactive = !(args.question && args.answer);

  if (interactive && !process.stdin.isTTY) {
    console.error('✖ Не хватает данных, а ввод недоступен (stdin не терминал).');
    console.error('  Передайте всё флагами:');
    console.error('  node tools/add-entry.js -i shot.png -q "Вопрос" -a "Ответ"');
    process.exit(1);
  }

  const rl = interactive
    ? readline.createInterface({ input: process.stdin, output: process.stdout })
    : null;

  try {
    let imagePath = args.image;
    if (!imagePath && interactive) {
      imagePath = await ask(rl, 'Путь к скриншоту (Enter — без картинки): ');
    }

    let question = args.question;
    if (!question) {
      question = await ask(rl, 'Вопрос: ');
    }
    if (!question) {
      console.error('✖ Вопрос обязателен — по нему идёт поиск.');
      process.exit(1);
    }

    let answer = args.answer;
    if (!answer) {
      answer = await askMultiline(rl, 'Ответ (можно несколько строк, пустая строка — конец):');
    }
    if (!answer) {
      console.error('✖ Ответ обязателен.');
      process.exit(1);
    }

    let screenshot = null;
    if (imagePath) {
      // Кавычки появляются при перетаскивании файла в терминал.
      const src = path.resolve(imagePath.replace(/^["']|["']$/g, ''));
      if (!fs.existsSync(src)) {
        console.error(`✖ Файл не найден: ${src}`);
        process.exit(1);
      }
      const ext = path.extname(src).toLowerCase();
      if (!ALLOWED_EXT.has(ext)) {
        console.error(`✖ Неподдерживаемый формат "${ext}". Допустимо: ${[...ALLOWED_EXT].join(', ')}`);
        process.exit(1);
      }
      const name = uniqueName(slugify(question), ext);
      fs.copyFileSync(src, path.join(SCREENS_DIR, name));
      screenshot = `screens/${name}`;
    }

    const data = readData();
    data.entries.push({
      id: Date.now().toString(36),
      question,
      answer,
      screenshot,
      createdAt: new Date().toISOString(),
    });
    writeData(data);

    console.log('\n✔ Запись добавлена');
    console.log(`  вопрос: ${question}`);
    console.log(`  скрин:  ${screenshot || '— (без картинки)'}`);
    console.log(`  всего:  ${data.entries.length}`);
    console.log('\nДальше: git add -A && git commit -m "archive: new entry" && git push');
  } finally {
    if (rl) rl.close();
  }
}

// ---------- точка входа ----------

const args = parseArgs(process.argv.slice(2));

if (args.flags.has('help') || args.flags.has('h')) {
  console.log(fs.readFileSync(__filename, 'utf8').split('*/')[0].replace(/^[\s\S]*?\/\*\*/, ''));
} else if (args.flags.has('list')) {
  cmdList();
} else if (args.remove) {
  cmdRemove(args.remove);
} else {
  cmdAdd(args);
}
