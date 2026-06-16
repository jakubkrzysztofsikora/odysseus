const express = require('express');
const router = express.Router();

router.get('/', (req, res) => {
  res.json({ message: 'Public API endpoint' });
});

// TODO: Add protected routes
// router.use('/protected', require('./protected'));

module.exports = router;
