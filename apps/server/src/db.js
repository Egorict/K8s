const { Pool } = require('pg');

const pool = new Pool({
  host: process.env.DB_HOST || 'localhost',
  port: parseInt(process.env.DB_PORT || '5432', 10),
  user: process.env.DB_USER || 'myuser',
  password: process.env.DB_PASSWORD || '',
  database: process.env.DB_NAME || 'mydb',
  max: 20,
});

const initDB = async () => {
  try {
    const client = await pool.connect();
    console.log('✅ Connected to PostgreSQL');
    await client.query(`
      CREATE TABLE IF NOT EXISTS items (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        value TEXT,
        "creatingDate" TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
      );
    `);
    console.log('✅ Table "items" is ready');
    client.release();
  } catch (err) {
    console.error('❌ Error initializing PostgreSQL:', err.message);
    throw err;
  }
};

const getAll = async () => {
  const res = await pool.query('SELECT * FROM items ORDER BY id;');
  return res.rows;
};

const getById = async (id) => {
  const res = await pool.query('SELECT * FROM items WHERE id = $1;', [id]);
  return res.rows[0] || null;
};

const createItem = async (name, value) => {
  const res = await pool.query(
    'INSERT INTO items (name, value, "creatingDate") VALUES ($1, $2, CURRENT_TIMESTAMP) RETURNING *;',
    [name, value || null]
  );
  return res.rows[0];
};

module.exports = { initDB, getAll, getById, createItem };