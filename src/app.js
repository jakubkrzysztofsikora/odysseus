const express = require('express');
const app = express();

// Middleware
app.use(express.json());

// Routes
app.use('/api', require('./routes/main'));

// Error handling
app.use((err, req, res, next) => {
  console.error(err.stack);
  res.status(500).send('Something broke!');
});

module.exports = app;
