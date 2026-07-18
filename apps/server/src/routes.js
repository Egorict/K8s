const express = require('express');
const { getAll, getById, createItem } = require('./db');

const router = express.Router();

// 1. Создать объект
router.post('/api/data', async (req, res) => {
  const { name, value } = req.body;
  if (!name) {
    return res.status(400).json({ error: 'name is required' });
  }
  try {
    const newItem = await createItem(name, value);
    res.status(201).json(newItem);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

// 2. Получить по id
router.get('/api/data/:id', async (req, res) => {
  const { id } = req.params;
  try {
    const item = await getById(Number(id));
    if (!item) {
      return res.status(404).json({ error: 'Not found' });
    }
    res.json(item);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

// 3. Получить все
router.get('/api/data', async (req, res) => {
  try {
    const items = await getAll();
    res.json(items);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

module.exports = router;