import { expect, test } from '@playwright/test';
import { setupApiMocks } from './fixtures.js';

/* C25 — MatchCandidates layout-regression.
 *
 * Tre buggar bekräftade i prod (2026-05-21):
 *  1. Långt filnamn spiller över kortets högerkant.
 *  2. Ensamt AI MATCH-kort ligger på vänster halva i stället för att
 *     fylla hela panelen — splitten ska ske först när YOUR PICK finns.
 *  3. Två-kolumn-läget blir för trångt vid smal viewport och måste
 *     stacka vertikalt under 1400px-breakpointen. */

const MISSING_ID = 12345;
const AI_MESSAGE_ID = 7;
const OTHER_MESSAGE_ID = 8;

const NOW_ISO = new Date().toISOString();
const LONG_FILE_NAME =
  '20260519_Finnair_Flyg_Helsingfors-Kopenhamn_tur-retur_' +
  'bokningsbekraftelse_slutgiltig_version.pdf';

function aiMessage(overrides = {}) {
  return {
    id: AI_MESSAGE_ID,
    message_id: 'm-7',
    sender: 'kvitto@finnair.com',
    subject: 'Flyg',
    file_name: '20260420 Finnair HEL-CPH.pdf',
    drive_file_id: 'drv-7',
    drive_link: 'https://drive/drv-7',
    status: 'saved',
    vendor: 'Finnair',
    amount: 554.5,
    currency: 'EUR',
    receipt_date: '2026-05-13',
    received_at: NOW_ISO,
    processed_at: NOW_ISO,
    category: 'Flyg',
    summary: 'Flygbiljett Helsingfors-Kopenhamn.',
    ai_description_en: 'Helsinki-Copenhagen flight.',
    ai_confidence: 95,
    bezala_upload_status: 'pending',
    bezala_transaction_id: null,
    bezala_error_message: null,
    deleted_at: null,
    delete_reason: null,
    pending_link: null,
    coupled: false,
    matched_bill_line_id: null,
    ...overrides,
  };
}

function otherMessage() {
  return {
    ...aiMessage(),
    id: OTHER_MESSAGE_ID,
    message_id: 'm-8',
    sender: 'kvitto@uber.com',
    vendor: 'Uber',
    file_name: '20260419 Uber HEL.pdf',
    amount: 15.9,
    receipt_date: '2026-04-19',
  };
}

function sampleSuggestions(aiOverrides = {}) {
  const ai = aiMessage(aiOverrides);
  return {
    missing_receipts: [
      {
        missing_receipt: {
          id: MISSING_ID,
          description: 'FINNAIR, HELSINKI',
          amount: 554.5,
          currency: 'EUR',
          date: '2026-05-13',
        },
        suggestions: [
          {
            message: ai,
            score: 95,
            score_breakdown: { amount: 50, date: 30, vendor: 15 },
          },
        ],
      },
    ],
    all_messages: [ai, otherMessage()],
  };
}

async function setupMocks(page, suggestions = sampleSuggestions()) {
  await setupApiMocks(page);

  await page.route('**/api/bezala/match-suggestions**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(suggestions),
    }),
  );

  await page.route('**/api/bezala/matched-pairs**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        pairs: [],
        total: 0,
        stats: { total_all_time: 0, this_week: 0, estimated_minutes_saved: 0 },
      }),
    }),
  );

  await page.route('**/api/feedback/match-result', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }),
  );
}

test.describe('MatchCandidates layout', () => {
  // Bred viewport (> 1400px) så split-läget är aktivt och testerna
  // bevisar JSX-logiken, inte bara media-query-stackningen.
  test.use({ viewport: { width: 1680, height: 1000 } });

  test('Bugg 2 — ensamt AI MATCH-kort fyller hela panelens bredd', async ({
    page,
  }) => {
    await setupMocks(page);
    await page.goto('/travel-tinder');

    const card = page.getByTestId('tt-candidate-ai');
    await expect(card).toBeVisible();
    // Ingen YOUR PICK vald → inget Card B.
    await expect(page.getByTestId('tt-candidate-user')).toHaveCount(0);

    const grid = page.locator('.tt-candidates__grid');
    await expect(grid).toHaveClass(/tt-candidates__grid--single/);

    const gridBox = await grid.boundingBox();
    const cardBox = await card.boundingBox();
    // Kortet fyller (nästan) hela griden — ingen reserverad tom kolumn.
    expect(cardBox.width).toBeGreaterThan(gridBox.width - 4);
  });

  test('Bugg 2 — split i två kolumner när YOUR PICK valts', async ({
    page,
  }) => {
    await setupMocks(page);
    await page.goto('/travel-tinder');

    await expect(page.getByTestId('tt-candidate-ai')).toBeVisible();
    await page.getByTestId(`tt-receipt-${OTHER_MESSAGE_ID}`).click();

    const aiCard = page.getByTestId('tt-candidate-ai');
    const userCard = page.getByTestId('tt-candidate-user');
    await expect(userCard).toBeVisible();

    const grid = page.locator('.tt-candidates__grid');
    await expect(grid).not.toHaveClass(/tt-candidates__grid--single/);

    const aiBox = await aiCard.boundingBox();
    const userBox = await userCard.boundingBox();
    // Sida vid sida: AI vänster om YOUR PICK, samma övre kant.
    expect(aiBox.x + aiBox.width).toBeLessThanOrEqual(userBox.x + 1);
    expect(Math.abs(aiBox.y - userBox.y)).toBeLessThan(8);
  });

  test('Bugg 1 — långt filnamn bryts inom kortet utan overflow', async ({
    page,
  }) => {
    await setupMocks(page, sampleSuggestions({ file_name: LONG_FILE_NAME }));
    await page.goto('/travel-tinder');

    const card = page.getByTestId('tt-candidate-ai');
    await expect(card).toBeVisible();

    const filename = card.locator('.tt-candidate__filename');
    await expect(filename).toContainText('Finnair');

    const cardBox = await card.boundingBox();
    const fnBox = await filename.boundingBox();
    // Filnamnets högerkant får inte gå utanför kortets högerkant (+1px
    // tolerans för subpixel-avrundning).
    expect(fnBox.x + fnBox.width).toBeLessThanOrEqual(
      cardBox.x + cardBox.width + 1,
    );
    // Långt filnamn utan mellanslag → måste ha brutits till flera rader.
    expect(fnBox.height).toBeGreaterThan(16);
  });

  test('Bugg 3 — kandidat-korten stackar vertikalt under 1400px', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1200, height: 1000 });
    await setupMocks(page);
    await page.goto('/travel-tinder');

    await expect(page.getByTestId('tt-candidate-ai')).toBeVisible();
    await page.getByTestId(`tt-receipt-${OTHER_MESSAGE_ID}`).click();

    const aiCard = page.getByTestId('tt-candidate-ai');
    const userCard = page.getByTestId('tt-candidate-user');
    await expect(userCard).toBeVisible();

    const aiBox = await aiCard.boundingBox();
    const userBox = await userCard.boundingBox();
    // Stackad: YOUR PICK ligger under AI MATCH, inte bredvid.
    expect(userBox.y).toBeGreaterThanOrEqual(aiBox.y + aiBox.height - 1);
  });
});
