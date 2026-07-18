const express = require('express');
const { pool } = require('./db');

const router = express.Router();

// 1. Создать объект
router.post('/api/data', async (req, res) => {
  const { name, value } = req.body;
  if (!name) {
    return res.status(400).json({ error: 'name is required' });
  }
  try {
    const result = await pool.query(
      'INSERT INTO saved_data (name, value) VALUES ($1, $2) RETURNING *',
      [name, value || null]
    );
    res.status(201).json(result.rows[0]);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

// 2. Получить объект по id
router.get('/api/data/:id', async (req, res) => {
  const { id } = req.params;
  try {
    const result = await pool.query(
      'SELECT * FROM saved_data WHERE id = $1',
      [id]
    );
    if (result.rows.length === 0) {
      return res.status(404).json({ error: 'Not found' });
    }
    res.json(result.rows[0]);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

// 3. Получить все объекты
router.get('/api/data', async (req, res) => {
  try {
    const result = await pool.query(
      'SELECT * FROM saved_data ORDER BY id'
    );
    res.json(result.rows);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

module.exports = router;