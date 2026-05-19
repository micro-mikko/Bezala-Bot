import { useCallback, useEffect, useMemo, useState } from 'react';
import { useI18n } from '../i18n/useI18n.jsx';
import { api, ApiError } from '../api/client.js';
import { useApiData } from '../hooks/useApiData.js';
import { useToast } from '../lib/toast.jsx';
import { fmtAmount } from '../lib/format.js';
import VendorLogo from '../components/VendorLogo.jsx';
import { IconLink } from '../icons/index.jsx';

/* FAS 5.4 — Kortmatchning.
 * Visar Bezalas saknade-kvitto-lista och föreslagna matchningar från
 * vår DB. Klick "Koppla ihop" anropar POST /match-to-bezala. */

const POLL_INTERVAL_MS = 5 * 60_000;

function ScoreBadge({ score }) {
  const tone = score >= 90 ? 'ok' : score >= 70 ? 'warn' : 'muted';
  return (
    <span className={`pill pill--${tone}`} data-testid={`score-${score}`}>
      <span className="mono">{score}</span>
    </span>
  );
}

function MissingItem({ row, isActive, onSelect }) {
  const { t, lang } = useI18n();
  return (
    <button
      type="button"
      className={`q-item ${isActive ? 'is-active' : ''}`}
      onClick={() => onSelect(row.missing_receipt.id)}
      aria-pressed={isActive}
      data-testid={`missing-item-${row.missing_receipt.id}`}
    >
      <VendorLogo name={row.missing_receipt.description} size={26} />
      <div className="q-item__body">
        <div className="q-item__vendor">
          {row.missing_receipt.description || (
            <span className="muted">{t.match.unknownVendor}</span>
          )}
        </div>
        <div className="q-item__meta">
          <span className="mono">{row.missing_receipt.date || '—'}</span>
          {' · '}
          <span className="mono">
            {row.suggestions.length} {t.match.suggestionsCount}
          </span>
        </div>
      </div>
      <div className="q-item__amount mono">
        {row.missing_receipt.amount != null
          ? fmtAmount(
              row.missing_receipt.amount,
              row.missing_receipt.currency,
              lang,
            )
          : '—'}
      </div>
    </button>
  );
}

function SuggestionRow({ suggestion, missing, onMatch, isMatching }) {
  const { t, lang } = useI18n();
  const m = suggestion.message;
  const conv = suggestion.conversion;
  return (
    <div className="match-suggestion" data-testid={`suggestion-${m.id}`}>
      <div className="match-suggestion__head">
        <ScoreBadge score={suggestion.score} />
        <span className="vchip">
          <VendorLogo name={m.vendor} />
          <span>{m.vendor || <span className="muted">—</span>}</span>
        </span>
        <span className="mono muted">{m.receipt_date || '—'}</span>
        <span className="mono">
          {m.amount != null ? fmtAmount(m.amount, m.currency, lang) : '—'}
        </span>
      </div>
      {conv ? (
        <div
          className="match-suggestion__conversion mono muted"
          data-testid={`conversion-${m.id}`}
        >
          {fmtAmount(conv.from_amount, conv.from_currency, lang)}
          {' ≈ '}
          {fmtAmount(conv.to_amount, conv.to_currency, lang)}
          {conv.date ? ` (ECB ${conv.date})` : ''}
        </div>
      ) : null}
      <div className="match-suggestion__file mono muted">
        {m.file_name || '—'}
      </div>
      <div className="match-suggestion__actions">
        <button
          type="button"
          className="btn primary"
          onClick={() => onMatch(m.id, missing.id)}
          disabled={isMatching}
          data-testid={`match-btn-${m.id}`}
        >
          <IconLink className="icon sm" />
          {isMatching ? t.match.matching : t.match.matchAction}
        </button>
      </div>
    </div>
  );
}

export default function Match() {
  const { t } = useI18n();
  const toast = useToast();
  const [activeId, setActiveId] = useState(null);
  const [matchingId, setMatchingId] = useState(null);

  const loader = useCallback(async () => {
    return api.bezalaMatchSuggestions();
  }, []);

  const { data, isLoading, refetch, error } = useApiData(loader, []);
  const rows = data || [];

  useEffect(() => {
    const id = setInterval(() => {
      refetch().catch(() => {});
    }, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [refetch]);

  // Auto-välj första saknade kvittot om inget är valt
  useEffect(() => {
    if (rows.length > 0 && activeId == null) {
      setActiveId(rows[0].missing_receipt.id);
    }
  }, [rows, activeId]);

  const active = useMemo(
    () => rows.find((r) => r.missing_receipt.id === activeId) || null,
    [rows, activeId],
  );

  const onMatch = useCallback(
    async (msgId, missingId) => {
      // C23 Del A — extra dubbel-klick-skydd. Knappen disables av
      // isMatching={matchingId === s.message.id} men React-renderingen
      // är async, så en snabb dubbel-klick kan annars trigga POSTen
      // två gånger innan knappen hinner bli disabled.
      if (matchingId != null) return;
      setMatchingId(msgId);
      try {
        await api.matchToBezala(msgId, missingId);
        toast.show({ kind: 'ok', message: t.match.toast.matched });
        // Ta bort raden ur listan + välj nästa
        refetch().catch(() => {});
        setActiveId(null);
      } catch (err) {
        // C23 Del A — 409 = annan ProcessedMessage hann före (race condition).
        // Visa informativ toast + refresha listan så användaren ser det
        // uppdaterade tillståndet.
        if (err instanceof ApiError && err.status === 409) {
          const info =
            err.body && typeof err.body === 'object' && typeof err.body.detail === 'object'
              ? err.body.detail
              : null;
          const vendor = info?.existing_vendor || '';
          toast.show({
            kind: 'warn',
            message: vendor
              ? `${t.match.toast.alreadyCoupled}: ${vendor}`
              : t.match.toast.alreadyCoupled,
          });
          refetch().catch(() => {});
          setActiveId(null);
          return;
        }
        const detail = err instanceof ApiError ? err.message : String(err);
        toast.show({
          kind: 'err',
          message: `${t.match.toast.matchFailed}: ${detail}`,
        });
      } finally {
        setMatchingId(null);
      }
    },
    [
      matchingId,
      refetch,
      t.match.toast.matchFailed,
      t.match.toast.matched,
      t.match.toast.alreadyCoupled,
      toast,
    ],
  );

  if (isLoading) {
    return (
      <div className="settings-loading muted" data-testid="match-loading">
        {t.common.loading}
      </div>
    );
  }

  if (error) {
    return (
      <div className="card card-pad" data-testid="match-error">
        <p className="muted">
          {t.match.loadFailed}: {String(error.message || error)}
        </p>
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div className="card card-pad" data-testid="match-empty">
        <p className="serif" style={{ fontSize: '20px', marginBottom: 8 }}>
          {t.match.empty.title}
        </p>
        <p className="muted">{t.match.empty.body}</p>
      </div>
    );
  }

  return (
    <div className="match-grid" data-testid="match-grid">
      <div className="queue">
        <div className="queue-head">
          <span className="queue-head__title">{t.match.listTitle}</span>
          <span className="pill pill--warn">
            <span className="pill__dot" aria-hidden="true" />
            <span className="mono">{rows.length}</span>
          </span>
        </div>
        <div className="queue-list" data-testid="match-list">
          {rows.map((row) => (
            <MissingItem
              key={row.missing_receipt.id}
              row={row}
              isActive={row.missing_receipt.id === activeId}
              onSelect={setActiveId}
            />
          ))}
        </div>
      </div>

      <div className="match-detail" data-testid="match-detail">
        {active ? (
          <>
            <div className="card card-pad match-detail__head">
              <div className="match-detail__title">
                {active.missing_receipt.description}
              </div>
              <div className="match-detail__meta mono muted">
                {active.missing_receipt.date} ·{' '}
                {active.missing_receipt.amount != null
                  ? fmtAmount(
                      active.missing_receipt.amount,
                      active.missing_receipt.currency,
                      'sv',
                    )
                  : '—'}
              </div>
            </div>
            {active.suggestions.length === 0 ? (
              <div className="card card-pad" data-testid="no-suggestions">
                <p className="muted">{t.match.noSuggestions}</p>
              </div>
            ) : (
              <div className="match-suggestions" data-testid="suggestion-list">
                {active.suggestions.map((s) => (
                  <SuggestionRow
                    key={s.message.id}
                    suggestion={s}
                    missing={active.missing_receipt}
                    onMatch={onMatch}
                    isMatching={matchingId === s.message.id}
                  />
                ))}
              </div>
            )}
          </>
        ) : (
          <div className="card card-pad muted">{t.match.selectPrompt}</div>
        )}
      </div>
    </div>
  );
}
