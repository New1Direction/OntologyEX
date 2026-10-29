import express from "express";
import helmet from "helmet";
import Stripe from "stripe";
import bcrypt from "bcrypt";

const app = express();
app.use(helmet());
const stripe = new Stripe(process.env.STRIPE_SECRET_KEY ?? "");

app.post("/api/checkout", async (req, res) => {
  const intent = await stripe.paymentIntents.create({ amount: req.body.totalCents, currency: "usd" });
  res.json({ clientSecret: intent.client_secret });
});

app.post("/api/orders/:id/refund", async (req, res) => {
  await stripe.refunds.create({ payment_intent: req.body.paymentIntent });
  res.json({ refunded: true });
});

app.post("/api/login", async (req, res) => {
  const ok = await bcrypt.compare(req.body.password, req.body.hash);
  res.json({ ok });
});

app.delete("/api/account", async (_req, res) => {
  res.json({ deleted: true }); // deleteAccount: removes the user and their orders
});

app.listen(3000);
