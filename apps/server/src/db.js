const fs = require('fs').promises;
const path = require('path');

const DATA_FILE = path.join(__dirname, '..', 'data', 'saved_data.json');

// Инициализация: создаём папку и файл, если их нет
const initDB = async () => {
  try {
    await fs.mkdir(path.dirname(DATA_FILE), { recursive: true });
    await fs.access(DATA_FILE).catch(async () => {
      await fs.writeFile(DATA_FILE, JSON.stringify([]));
    });
    console.log('✅ JSON data file ready');
  } catch (err) {
    console.error('❌ Error initializing JSON file:', err.message);
  }
};

// Получить все записи
const getAll = async () => {
  const data = await fs.readFile(DATA_FILE, 'utf-8');
  return JSON.parse(data);
};

// Получить по id
const getById = async (id) => {
  const items = await getAll();
  return items.find(item => item.id === id);
};

// Создать новую запись
const createItem = async (name, value) => {
  const items = await getAll();
  const newItem = {
    id: items.length > 0 ? Math.max(...items.map(i => i.id)) + 1 : 1,
    name,
    value: value || null,
    creatingDate: new Date().toISOString(),
  };
  items.push(newItem);
  await fs.writeFile(DATA_FILE, JSON.stringify(items, null, 2));
  return newItem;
};

module.exports = { initDB, getAll, getById, createItem };