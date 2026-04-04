require('dotenv').config();
const express = require('express');
const multer = require('multer');
const fs = require('fs');
const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);
const cookieParser = require('cookie-parser');

const app = express();
const upload = multer({ dest: 'uploads/' });

app.use(express.static('public'));
app.use(express.json());
app.use(cookieParser());

const sessions = {};

function getSession(sessionId) {
  if (!sessions[sessionId]) {
    sessions[sessionId] = { uploads: 0, isPro: false, isPremium: false };
  }
  return sessions[sessionId];
}

app.post('/create-checkout', async (req, res) => {
  try {
    const { type } = req.body;
    const sessionId = req.cookies.studly_session || Math.random().toString(36).substr(2, 9);
    const isSubscription = type === 'subscription' || type === 'premium';
    const amount = type === 'trial' ? 299 : type === 'subscription' ? 999 : 1299;
    const name = type === 'trial' ? 'Studly — Premier essai' : type === 'subscription' ? 'Studly Pro' : 'Studly Premium';
    const description = type === 'trial' ? 'Génère ta première fiche' : type === 'subscription' ? 'Fiches et QCM illimités' : 'Fiches, QCM et Flashcards illimités';

    const session = await stripe.checkout.sessions.create({
      payment_method_types: ['card'],
      mode: isSubscription ? 'subscription' : 'payment',
      line_items: [{
        price_data: {
          currency: 'eur',
          product_data: { name, description },
          unit_amount: amount,
          ...(isSubscription && { recurring: { interval: 'month' } }),
        },
        quantity: 1,
      }],
      success_url: `http://localhost:3000/success?session_id={CHECKOUT_SESSION_ID}&user_session=${sessionId}&type=${type}`,
      cancel_url: `http://localhost:3000`,
    });

    res.json({ url: session.url });
  } catch (error) {
    console.error(error);
    res.status(500).json({ error: error.message });
  }
});

app.get('/success', async (req, res) => {
  const { session_id, user_session, type } = req.query;
  try {
    const stripeSession = await stripe.checkout.sessions.retrieve(session_id);
    if (stripeSession.payment_status === 'paid' || stripeSession.status === 'complete') {
      const userSession = getSession(user_session);
      if (type === 'premium') {
        userSession.isPro = true;
        userSession.isPremium = true;
      } else if (type === 'subscription') {
        userSession.isPro = true;
        userSession.isPremium = false;
      } else {
        userSession.uploads = 0;
      }
    }
    res.cookie('studly_session', user_session, { maxAge: 30 * 24 * 60 * 60 * 1000, httpOnly: false });
    res.redirect(`/?type=${type}`);
  } catch (error) {
    console.error(error);
    res.redirect('/');
  }
});

app.post('/check-access', (req, res) => {
  const sessionId = req.cookies.studly_session || req.body.sessionId || Math.random().toString(36).substr(2, 9);
  const userSession = getSession(sessionId);
  res.cookie('studly_session', sessionId, { maxAge: 30 * 24 * 60 * 60 * 1000, httpOnly: false });
  res.json({ ...userSession, sessionId });
});

app.post('/analyze', upload.single('pdf'), async (req, res) => {
  try {
    const sessionId = req.cookies.studly_session || req.body.sessionId;
    const userSession = getSession(sessionId);

    if (!userSession.isPro && userSession.uploads >= 1) {
      if (req.file) fs.unlinkSync(req.file.path);
      return res.status(403).json({ error: 'limit_reached' });
    }

    const fileBuffer = fs.readFileSync(req.file.path);
    const base64PDF = fileBuffer.toString('base64');

    const response = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': process.env.ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
        'anthropic-beta': 'pdfs-2024-09-25'
      },
      body: JSON.stringify({
        model: 'claude-opus-4-5',
        max_tokens: 4000,
        messages: [{
          role: 'user',
          content: [
            {
              type: 'document',
              source: {
                type: 'base64',
                media_type: 'application/pdf',
                data: base64PDF
              }
            },
            {
              type: 'text',
              text: `Tu es un assistant pédagogique expert français. Analyse ce cours et génère une fiche de révision complète.

Réponds UNIQUEMENT en JSON valide avec ce format :
{
  "matiere": "nom de la matière",
  "titre": "titre du cours",
  "couleur": "une couleur parmi : purple, yellow, pink, blue, green",
  "fiche": [
    {
      "titre": "Titre du point clé",
      "contenu": "Explication détaillée avec exemples et chiffres",
      "type": "normal"
    },
    {
      "titre": "Titre avec flèche",
      "contenu": "Explication qui découle du point précédent",
      "type": "arrow"
    },
    {
      "titre": "Point important",
      "contenu": "Explication d'un point crucial à retenir absolument",
      "type": "important"
    }
  ],
  "qcm": [
    {
      "question": "Question précise ?",
      "choix": ["A. choix 1", "B. choix 2", "C. choix 3", "D. choix 4"],
      "reponse": "A"
    }
  ],
  "flashcards": [
    {
      "question": "Qu'est-ce que... ?",
      "reponse": "C'est... Exemple : ..."
    }
  ]
}

Règles :
- Adapte le nombre de points selon la richesse du cours (3-5 pour court, 6-8 pour moyen, 8-12 pour long)
- Chaque point doit avoir un titre court et un contenu détaillé avec exemples
- Utilise le type "arrow" pour les points qui découlent d un autre
- Utilise le type "important" pour les définitions ou formules clés
- Utilise le type "normal" pour les autres points
- 5 questions QCM précises basées sur le cours
- 8 à 12 flashcards avec questions courtes et réponses détaillées avec exemples
Aucun texte avant ou après le JSON.`
            }
          ]
        }]
      })
    });

    const data = await response.json();

    if (!data.content || !data.content[0]) {
      console.error('Erreur API:', JSON.stringify(data));
      return res.status(500).json({ error: 'Erreur API Anthropic' });
    }

    const content = data.content[0].text;
    const clean = content.replace(/```json|```/g, '').trim();
    const result = JSON.parse(clean);
    result.isPremium = userSession.isPremium;

    userSession.uploads += 1;
    fs.unlinkSync(req.file.path);
    res.cookie('studly_session', sessionId, { maxAge: 30 * 24 * 60 * 60 * 1000, httpOnly: false });
    res.json(result);

  } catch (error) {
    console.error('Erreur complète:', error);
    res.status(500).json({ error: error.message });
  }
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Studly tourne sur http://localhost:${PORT}`);
});