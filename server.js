const express = require('express');

const app = express();
const port = process.env.PORT || 8080;

app.use(express.json());

app.get('/', (_req, res) => {
  res.send('Hello World!');
});

app.post('/api/test', (req, res) => {
  const { name, phone, timestamp } = req.body;
  console.log(`[${timestamp}] Name: ${name} | Phone: ${phone}`);
  res.sendStatus(200);
});

app.listen(port, () => {
  console.log(`Server running at http://localhost:${port}/`);
});
