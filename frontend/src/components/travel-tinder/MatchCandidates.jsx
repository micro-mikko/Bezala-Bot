import { useState } from 'react';
import { useI18n } from '../../i18n/useI18n.jsx';
import { fmtAmount } from '../../lib/format.js';
import VendorLogo from '../VendorLogo.jsx';

/* FAS 5.16 — kandidat-kort i höger panel. Två kort sida-vid-sida:
 * AI MATCH (Card A) och YOUR PICK (Card B). Klick på kortet gör inget;
 * endast knapparna "Open in Drawer" och "Couple →" agerar. Modal är
 * borttagen — koppling sker direkt via Couple-knappen.
 *
 * När AI-förslaget och användarens val är samma kvitto renderas ett
 * sammansatt kort "⭐ AI MATCH · matches your pick" med en enda
 * Couple-knapp. */

function CandidateCard({
  variant,           // 'ai' | 'user' | 'merged'
  message,
  score,             // number | null (only for ai/merged)
  onCouple,
  onOpenDrawer,
  onClear,           // only for user variant
  coupling,
  testIdSuffix,
}) {
  const { t, lang } = useI18n();
  const [showDetails, setShowDetails] = useState(false);

  const headerLabel =
    variant === 'ai'
      ? t.travelTinder.candidates.aiHeader
      : variant === 'merged'
        ? t.travelTinder.candidates.mergedHeader
        : t.travelTinder.candidates.userHeader;

  const displayScore =
    score != null ? Math.min(100, Math.max(0, Math.round(score))) : null;

  return (
    <article
      className={`tt-candidate tt-candidate--${variant}`}
      data-testid={`tt-candidate-${testIdSuffix}`}
    >
      <header className="tt-candidate__head">
        <span className="tt-candidate__badge mono">{headerLabel}</span>
        {displayScore != null ? (
          <span className="tt-candidate__score mono">{displayScore}%</span>
        ) : null}
        {variant === 'user' && onClear ? (
          <button
            type="button"
            className="tt-candidate__close"
            onClick={onClear}
            aria-label={t.travelTinder.candidates.clear}
            data-testid="tt-candidate-clear"
          >
            ×
          </button>
        ) : null}
      </header>

      <div className="tt-candidate__vendor">
        <VendorLogo name={message.vendor || message.sender} size={28} />
        <div className="tt-candidate__vendor-body">
          <div className="tt-candidate__vendor-name">
            {message.vendor || <span className="muted">—</span>}
          </div>
          <div className="tt-candidate__vendor-amount mono">
            {message.amount != null
              ? fmtAmount(message.amount, message.currency, lang)
              : '—'}
          </div>
        </div>
      </div>

      <dl className="tt-candidate__meta">
        <div>
          <dt>{t.travelTinder.candidates.date}</dt>
          <dd className="mono">{message.receipt_date || '—'}</dd>
        </div>
        <div>
          <dt>{t.travelTinder.candidates.file}</dt>
          <dd className="mono muted tt-candidate__filename">
            {message.file_name || (
              <span className="muted">{t.travelTinder.pdfMissing}</span>
            )}
          </dd>
        </div>
      </dl>

      <button
        type="button"
        className="tt-candidate__details-toggle"
        onClick={() => setShowDetails((v) => !v)}
        data-testid="tt-candidate-details-toggle"
        aria-expanded={showDetails}
      >
        {t.travelTinder.candidates.details} {showDetails ? '▴' : '▾'}
      </button>

      {showDetails ? (
        <div className="tt-candidate__details">
          {message.category ? (
            <div className="tt-candidate__detail-row">
              <span className="muted">
                {t.travelTinder.candidates.category}
              </span>
              <span>{message.category}</span>
            </div>
          ) : null}
          {message.summary ? (
            <p className="tt-candidate__summary">{message.summary}</p>
          ) : null}
          {message.ai_description_en ? (
            <p className="tt-candidate__summary tt-candidate__summary--subtle">
              {message.ai_description_en}
            </p>
          ) : null}
          {!message.category && !message.summary && !message.ai_description_en ? (
            <p className="muted small">
              {t.travelTinder.candidates.noDetails}
            </p>
          ) : null}
        </div>
      ) : null}

      <div className="tt-candidate__actions">
        <button
          type="button"
          className="btn ghost"
          onClick={onOpenDrawer}
          disabled={coupling}
          data-testid="tt-candidate-open-drawer"
        >
          {t.travelTinder.candidates.openInDrawer}
        </button>
        <button
          type="button"
          className="btn primary tt-candidate-couple-btn"
          onClick={onCouple}
          disabled={coupling}
          aria-busy={coupling || undefined}
          data-testid="tt-candidate-couple"
        >
          {coupling ? (
            <>
              <span
                className="tt-spinner"
                aria-hidden="true"
                data-testid="tt-candidate-couple-spinner"
              />
              <span>{t.match.matching}</span>
            </>
          ) : (
            t.travelTinder.candidates.couple + ' →'
          )}
        </button>
      </div>
    </article>
  );
}

export default function MatchCandidates({
  aiSuggestion,           // { message, score, score_breakdown } | null
  userPickMessage,        // ProcessedMessage | null
  paymentId,              // bill_line_id captured at this render — locks
                          //   the target so a silent refresh shuffling
                          //   `selected` under us can't divert the Couple
                          //   click to a stranger bill_line (C20 bug).
  onClearUserPick,
  onCouple,               // (message, paymentId, aiContext|null) => void
  onOpenDrawer,           // (message) => void
  coupling,
}) {
  const { t } = useI18n();
  const aiMessage = aiSuggestion?.message || null;
  const aiScore = aiSuggestion?.score ?? null;
  const aiBreakdown = aiSuggestion?.score_breakdown || null;

  const samePick =
    aiMessage && userPickMessage && aiMessage.id === userPickMessage.id;

  if (!aiMessage && !userPickMessage) {
    return (
      <div className="tt-candidates">
        <div className="tt-section-head">
          <h3>{t.travelTinder.candidates.title}</h3>
        </div>
        <div className="tt-empty-card" data-testid="tt-no-suggestion">
          <h3>{t.travelTinder.empty.noSuggestion}</h3>
          <p className="muted">{t.travelTinder.empty.noSuggestionBody}</p>
        </div>
      </div>
    );
  }

  if (samePick) {
    return (
      <div className="tt-candidates">
        <div className="tt-section-head">
          <h3>{t.travelTinder.candidates.title}</h3>
        </div>
        <div className="tt-candidates__grid tt-candidates__grid--single">
          <CandidateCard
            variant="merged"
            message={aiMessage}
            score={aiScore}
            onCouple={() =>
              onCouple(aiMessage, paymentId, {
                score: aiScore,
                score_breakdown: aiBreakdown,
              })
            }
            onOpenDrawer={() => onOpenDrawer(aiMessage)}
            onClear={onClearUserPick}
            coupling={coupling}
            testIdSuffix="merged"
          />
        </div>
      </div>
    );
  }

  // Bug 2 — a lone card (only AI MATCH, or only YOUR PICK) must fill the
  // full panel width. The two-column split is reserved for when both
  // cards are shown side by side.
  const cardCount = (aiMessage ? 1 : 0) + (userPickMessage ? 1 : 0);

  return (
    <div className="tt-candidates">
      <div className="tt-section-head">
        <h3>{t.travelTinder.candidates.title}</h3>
      </div>
      <div
        className={`tt-candidates__grid${
          cardCount === 1 ? ' tt-candidates__grid--single' : ''
        }`}
      >
        {aiMessage ? (
          <CandidateCard
            variant="ai"
            message={aiMessage}
            score={aiScore}
            onCouple={() =>
              onCouple(aiMessage, paymentId, {
                score: aiScore,
                score_breakdown: aiBreakdown,
              })
            }
            onOpenDrawer={() => onOpenDrawer(aiMessage)}
            coupling={coupling}
            testIdSuffix="ai"
          />
        ) : null}
        {userPickMessage ? (
          <CandidateCard
            variant="user"
            message={userPickMessage}
            score={null}
            onCouple={() => onCouple(userPickMessage, paymentId, null)}
            onOpenDrawer={() => onOpenDrawer(userPickMessage)}
            onClear={onClearUserPick}
            coupling={coupling}
            testIdSuffix="user"
          />
        ) : null}
      </div>
    </div>
  );
}
