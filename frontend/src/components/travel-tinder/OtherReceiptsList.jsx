import { useMemo } from 'react';
import { useI18n } from '../../i18n/useI18n.jsx';
import { fmtAmount } from '../../lib/format.js';
import VendorLogo from '../VendorLogo.jsx';

/* Höger panel: toolbar (sök/filter/sort), match-kandidat-block (slot),
 * listrubrik "Andra kvitton" och raderna. Coupled-rader visas grayade om
 * statusFilter inte exkluderar dem. AI-förslaget filtreras bort från
 * andra-listan eftersom det visas separat ovanför.
 *
 * När ingen korttrans är vald visas UploadCard som default-content
 * istället för kandidat-korten. */

const DATE_BUCKETS = {
  '7d': 7,
  '30d': 30,
  '90d': 90,
  all: null,
};

function withinDays(iso, days) {
  if (days == null) return true;
  if (!iso) return false;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return false;
  const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
  return d.getTime() >= cutoff;
}

function compareBy(a, b, key, dir) {
  const sign = dir === 'asc' ? 1 : -1;
  const av = a[key];
  const bv = b[key];
  if (av == null && bv == null) return 0;
  if (av == null) return 1;
  if (bv == null) return -1;
  if (typeof av === 'number' && typeof bv === 'number') {
    return (av - bv) * sign;
  }
  return String(av).localeCompare(String(bv)) * sign;
}

function ReceiptRow({ message, onClick, isAiSuggestion, isSelectedCandidate, score }) {
  const { t, lang } = useI18n();
  const coupled = message.coupled;
  const classes = [
    'tt-receipt',
    coupled ? 'is-coupled' : '',
    isAiSuggestion ? 'is-ai-suggestion' : '',
    isSelectedCandidate ? 'is-selected-candidate' : '',
  ]
    .filter(Boolean)
    .join(' ');
  const coupledTooltip = coupled
    ? (t.travelTinder.coupledTooltip || '').replace(
        '{txId}',
        message.bezala_transaction_id != null
          ? String(message.bezala_transaction_id)
          : '—',
      )
    : null;
  return (
    <button
      type="button"
      className={classes}
      onClick={() => onClick(message)}
      data-testid={`tt-receipt-${message.id}`}
      data-coupled={coupled || false}
      data-selected={isSelectedCandidate || false}
      title={coupledTooltip || undefined}
      aria-label={coupledTooltip || undefined}
    >
      <VendorLogo name={message.vendor || message.sender} size={20} />
      <div className="tt-receipt__body">
        <div className="tt-receipt__vendor">
          {isAiSuggestion ? <span className="tt-receipt__star">⭐ </span> : null}
          {message.vendor || message.file_name || (
            <span className="muted">—</span>
          )}
          {coupled ? (
            <span
              className="tt-receipt__tag tt-receipt__tag--coupled mono"
              data-testid={`tt-receipt-coupled-${message.id}`}
            >
              🔗 {t.travelTinder.coupledTag}
            </span>
          ) : null}
          {isSelectedCandidate ? (
            <span className="tt-receipt__tag tt-receipt__tag--selected mono">
              {t.travelTinder.candidates.selectedBadge}
            </span>
          ) : null}
          {isAiSuggestion ? (
            <span className="tt-receipt__score mono">
              {Math.min(
                100,
                Math.max(0, Math.round(((score ?? 0) / 110) * 100)),
              )}%
            </span>
          ) : null}
        </div>
        <div className="tt-receipt__meta mono muted">
          {message.receipt_date || '—'}
        </div>
      </div>
      <div className="tt-receipt__amount mono">
        {message.amount != null
          ? fmtAmount(message.amount, message.currency, lang)
          : '—'}
      </div>
    </button>
  );
}

export default function OtherReceiptsList({
  allMessages,
  selected,
  activeSuggestion,
  selectedCandidateMessageId,
  onClickReceipt,
  onShowPdfPreview, // eslint-disable-line no-unused-vars
  search,
  setSearch,
  statusFilter,
  setStatusFilter,
  dateFilter,
  setDateFilter,
  currencyFilter,
  setCurrencyFilter,
  sortBy,
  setSortBy,
  sortDir,
  setSortDir,
  tinderCard,
  uploadCard,
  isLoading,
}) {
  const { t } = useI18n();

  const currencies = useMemo(() => {
    const set = new Set();
    for (const m of allMessages) {
      if (m.currency) set.add(m.currency);
    }
    return Array.from(set).sort();
  }, [allMessages]);

  const aiSuggestionId = activeSuggestion?.message?.id ?? null;

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const days = DATE_BUCKETS[dateFilter] ?? null;
    return allMessages.filter((m) => {
      if (m.id === aiSuggestionId) return false; // visas i tinder-kort
      if (statusFilter === 'uncoupled' && m.coupled) return false;
      if (statusFilter === 'coupled' && !m.coupled) return false;
      if (currencyFilter !== 'all' && m.currency !== currencyFilter) {
        return false;
      }
      if (!withinDays(m.received_at || m.processed_at, days)) return false;
      if (q) {
        const hay = [
          m.vendor,
          m.file_name,
          m.amount != null ? String(m.amount) : '',
          m.summary,
          m.sender,
          m.subject,
        ]
          .filter(Boolean)
          .join(' ')
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [allMessages, aiSuggestionId, search, statusFilter, dateFilter, currencyFilter]);

  const sorted = useMemo(() => {
    const arr = filtered.slice();
    arr.sort((a, b) => compareBy(a, b, sortBy, sortDir));
    return arr;
  }, [filtered, sortBy, sortDir]);

  return (
    <div className="tt-receipts" data-testid="tt-receipts">
      <div className="tt-receipts__main">
        {selected ? (
          tinderCard
        ) : (
          <div className="tt-empty-card" data-testid="tt-select-prompt">
            <h3>🎯 {t.travelTinder.selectPrompt}</h3>
            <p className="muted">── {t.travelTinder.or} ──</p>
            {uploadCard}
          </div>
        )}

        <div className="tt-section-head">
          <h3>{t.travelTinder.otherReceipts}</h3>
          <span className="muted mono" data-testid="tt-receipts-count">
            {t.travelTinder.receiptsCount
              .replace('{shown}', String(sorted.length))
              .replace(
                '{total}',
                String(allMessages.filter((m) => m.id !== aiSuggestionId).length),
              )}
          </span>
        </div>

        <div className="tt-receipts__toolbar">
          <div className="tt-receipts__toolbar-row tt-receipts__toolbar-row--search">
            <input
              type="search"
              className="tt-search"
              placeholder={t.travelTinder.search}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              data-testid="tt-search"
              aria-label={t.travelTinder.search}
            />
          </div>
          <div className="tt-receipts__toolbar-row tt-receipts__toolbar-row--filters">
            <select
              className="tt-select"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              data-testid="tt-filter-status"
              aria-label={t.travelTinder.filterStatus}
            >
              <option value="all">{t.travelTinder.filterStatusAll}</option>
              <option value="uncoupled">
                {t.travelTinder.filterStatusUncoupled}
              </option>
              <option value="coupled">{t.travelTinder.filterStatusCoupled}</option>
            </select>
            <select
              className="tt-select"
              value={dateFilter}
              onChange={(e) => setDateFilter(e.target.value)}
              data-testid="tt-filter-date"
              aria-label={t.travelTinder.filterDate}
            >
              <option value="7d">{t.travelTinder.filterDate7d}</option>
              <option value="30d">{t.travelTinder.filterDate30d}</option>
              <option value="90d">{t.travelTinder.filterDate90d}</option>
              <option value="all">{t.travelTinder.filterDateAll}</option>
            </select>
            <select
              className="tt-select"
              value={currencyFilter}
              onChange={(e) => setCurrencyFilter(e.target.value)}
              data-testid="tt-filter-currency"
              aria-label={t.travelTinder.filterCurrency}
            >
              <option value="all">{t.travelTinder.filterStatusAll}</option>
              {currencies.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
            <span className="tt-receipts__toolbar-spacer" aria-hidden="true" />
            <select
              className="tt-select"
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              data-testid="tt-sort-by"
              aria-label={t.travelTinder.sort}
            >
              <option value="processed_at">{t.travelTinder.sortProcessed}</option>
              <option value="receipt_date">{t.travelTinder.sortDate}</option>
              <option value="amount">{t.travelTinder.sortAmount}</option>
              <option value="vendor">{t.travelTinder.sortVendor}</option>
            </select>
            <button
              type="button"
              className="tt-sort-dir"
              onClick={() => setSortDir(sortDir === 'desc' ? 'asc' : 'desc')}
              data-testid="tt-sort-dir"
              aria-label={
                sortDir === 'desc' ? t.travelTinder.sortDesc : t.travelTinder.sortAsc
              }
              title={
                sortDir === 'desc' ? t.travelTinder.sortDesc : t.travelTinder.sortAsc
              }
            >
              {sortDir === 'desc' ? '↓' : '↑'}
            </button>
          </div>
        </div>

        {isLoading ? (
          <div className="muted">{t.common.loading}</div>
        ) : sorted.length === 0 ? (
          <div className="tt-empty-card" data-testid="tt-receipts-empty">
            <h3>{t.travelTinder.empty.noReceipts}</h3>
            <p className="muted">{t.travelTinder.empty.noReceiptsBody}</p>
          </div>
        ) : (
          <div className="tt-receipts__list">
            {sorted.map((m) => (
              <ReceiptRow
                key={m.id}
                message={m}
                onClick={onClickReceipt}
                isAiSuggestion={false}
                isSelectedCandidate={m.id === selectedCandidateMessageId}
                score={null}
              />
            ))}
          </div>
        )}

        {selected ? uploadCard : null}
      </div>
    </div>
  );
}
