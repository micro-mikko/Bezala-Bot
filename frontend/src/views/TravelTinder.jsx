/* FAS 8.5a — Travel Tinder. Tinder-stil-koppling av Bezala-korttrans
 * mot kvitton. Vänster panel = saknade korttransaktioner, höger panel
 * = AI-förslag som stort kort + lista med alla kvitton.
 *
 * Är en PARALLELL vy bredvid Översikt + Kortmatchning. När den är
 * verifierad rivs gamla vyerna i FAS 8.5b.
 *
 * State:
 *  - selectedPayment: vald korttrans (objektet, inte bara id)
 *  - all_messages: alla saved-rader (inkl. kopplade) från backend
 *  - missing_receipts: saknade Bezala-kortrader + AI-suggestions
 *  - search/filter/sort persistas i localStorage (tt_*)
 *
 * Auto-refresh var 10 min. Network-fel renderas som toast utan att
 * tappa cachen.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useI18n } from '../i18n/useI18n.jsx';
import { api, ApiError } from '../api/client.js';
import { useToast } from '../lib/toast.jsx';
import { useDrawer } from '../drawer/DrawerProvider.jsx';
import MissingPaymentsList from '../components/travel-tinder/MissingPaymentsList.jsx';
import OtherReceiptsList from '../components/travel-tinder/OtherReceiptsList.jsx';
import MatchCandidates from '../components/travel-tinder/MatchCandidates.jsx';
import UploadCard from '../components/travel-tinder/UploadCard.jsx';
import PdfPreviewLightbox from '../components/travel-tinder/PdfPreviewLightbox.jsx';
import MatchedPairsList from '../components/travel-tinder/MatchedPairsList.jsx';
import { IconRefresh } from '../icons/index.jsx';

const REFRESH_INTERVAL_MS = 10 * 60 * 1000;
const LS_KEY = 'tt_state_v1';
const LS_WIDTH_KEY = 'tt_panel_width';
const LS_MODE_KEY = 'tt_mode';
const LS_MATCHED_PERIOD_KEY = 'tt_matched_period';
const LS_MATCHED_SEARCH_KEY = 'tt_matched_search';
const PANEL_WIDTH_DEFAULT = 300;
const PANEL_WIDTH_MIN = 200;
const PANEL_WIDTH_MAX = 500;
const MOBILE_BREAKPOINT = 800;

function clampWidth(n) {
  if (typeof n !== 'number' || Number.isNaN(n)) return PANEL_WIDTH_DEFAULT;
  return Math.min(PANEL_WIDTH_MAX, Math.max(PANEL_WIDTH_MIN, Math.round(n)));
}

function loadPanelWidth() {
  if (typeof window === 'undefined') return PANEL_WIDTH_DEFAULT;
  try {
    const raw = window.localStorage.getItem(LS_WIDTH_KEY);
    if (!raw) return PANEL_WIDTH_DEFAULT;
    const n = Number.parseInt(raw, 10);
    return Number.isFinite(n) ? clampWidth(n) : PANEL_WIDTH_DEFAULT;
  } catch {
    return PANEL_WIDTH_DEFAULT;
  }
}

function persistPanelWidth(px) {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(LS_WIDTH_KEY, String(clampWidth(px)));
  } catch {
    // tappa tyst
  }
}

function loadPersisted() {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(LS_KEY);
    return raw ? JSON.parse(raw) || {} : {};
  } catch {
    return {};
  }
}

function persist(patch) {
  if (typeof window === 'undefined') return;
  try {
    const cur = loadPersisted();
    window.localStorage.setItem(LS_KEY, JSON.stringify({ ...cur, ...patch }));
  } catch {
    // localStorage kan vara full eller blockerad — tappa tyst.
  }
}

function formatRelative(ts, t) {
  if (!ts) return t.travelTinder.justNow;
  const diff = Math.max(0, Math.floor((Date.now() - ts) / 60000));
  if (diff < 1) return t.travelTinder.justNow;
  return t.travelTinder.minutesAgo.replace('{n}', String(diff));
}

export default function TravelTinder() {
  const { t } = useI18n();
  const toast = useToast();
  const { openDrawer } = useDrawer();
  const persisted = useRef(loadPersisted()).current;

  const [data, setData] = useState({ missing_receipts: [], all_messages: [] });
  const [isLoading, setIsLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState(null);
  const [refreshing, setRefreshing] = useState(false);

  const [selectedPaymentId, setSelectedPaymentId] = useState(null);
  const [skippedSuggestionIds, setSkippedSuggestionIds] = useState([]);
  // FAS 5.16 — användarens manuellt valda kandidat-kvitto. null = bara
  // AI-kortet visas. Rensas vid: × i Card B, samma rad klickad igen,
  // lyckad koppling, eller byte av korttrans i vänster panel.
  const [selectedCandidateMessageId, setSelectedCandidateMessageId] =
    useState(null);

  const [searchQuery, setSearchQuery] = useState(persisted.search || '');
  const [statusFilter, setStatusFilter] = useState(
    persisted.statusFilter || 'uncoupled',
  );
  const [dateFilter, setDateFilter] = useState(persisted.dateFilter || '30d');
  const [currencyFilter, setCurrencyFilter] = useState(
    persisted.currencyFilter || 'all',
  );
  const [sortBy, setSortBy] = useState(persisted.sortBy || 'processed_at');
  const [sortDir, setSortDir] = useState(persisted.sortDir || 'desc');

  const [matching, setMatching] = useState(false);
  // Synchronous re-entry guard. setMatching(true) is async — a rapid
  // second click on Couple can fire onConfirm before React re-renders
  // with disabled=true. The ref blocks that path synchronously.
  const matchingLockRef = useRef(false);
  const [pdfPreviewMessage, setPdfPreviewMessage] = useState(null);

  // C14 — optimistic UI för Couple. inFlightPaymentIds = vilka rader
  // som har en POST i flight (deemfas + "kopplar…"-indikator).
  // completedPaymentIds = rader som lokalt klarats men där refresh inte
  // hunnit synka från servern — dölj direkt så användaren får
  // omedelbar bekräftelse.
  const [inFlightPaymentId, setInFlightPaymentId] = useState(null);
  const [completedPaymentIds, setCompletedPaymentIds] = useState(
    () => new Set(),
  );

  // FAS 8.5 — Matchade-vy state. mode persistas separat så användaren
  // hamnar tillbaka i rätt läge nästa session.
  const [mode, setMode] = useState(() => {
    try {
      const raw = window.localStorage.getItem(LS_MODE_KEY);
      return raw === 'matched' ? 'matched' : 'unmatched';
    } catch {
      return 'unmatched';
    }
  });
  const [matchedPeriod, setMatchedPeriod] = useState(() => {
    try {
      const raw = window.localStorage.getItem(LS_MATCHED_PERIOD_KEY);
      return ['7d', '30d', '90d', 'all'].includes(raw) ? raw : '30d';
    } catch {
      return '30d';
    }
  });
  const [matchedSearch, setMatchedSearch] = useState(() => {
    try {
      return window.localStorage.getItem(LS_MATCHED_SEARCH_KEY) || '';
    } catch {
      return '';
    }
  });
  const [matchedData, setMatchedData] = useState({
    pairs: [],
    total: 0,
    stats: { total_all_time: 0, this_week: 0, estimated_minutes_saved: 0 },
  });
  const [matchedLoading, setMatchedLoading] = useState(false);

  // Resizable splitter — bredd persistas separat (egen LS-nyckel) så
  // den överlever även om huvud-state-objektet byter shape framöver.
  const [panelWidth, setPanelWidth] = useState(() => loadPanelWidth());
  const isDraggingRef = useRef(false);
  const dragStartRef = useRef({ x: 0, w: PANEL_WIDTH_DEFAULT });

  const onSplitterMouseDown = useCallback(
    (e) => {
      e.preventDefault();
      isDraggingRef.current = true;
      dragStartRef.current = { x: e.clientX, w: panelWidth };
      document.body.style.cursor = 'col-resize';
      // Förhindra textmarkering under drag.
      document.body.style.userSelect = 'none';
    },
    [panelWidth],
  );

  useEffect(() => {
    function onMove(e) {
      if (!isDraggingRef.current) return;
      const delta = e.clientX - dragStartRef.current.x;
      setPanelWidth(clampWidth(dragStartRef.current.w + delta));
    }
    function onUp() {
      if (!isDraggingRef.current) return;
      isDraggingRef.current = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      // Persistera bara vid släpp — undvik att hamra localStorage under drag.
      setPanelWidth((w) => {
        persistPanelWidth(w);
        return w;
      });
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, []);

  const onSplitterKeyDown = useCallback((e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    const step = e.shiftKey ? 32 : 8;
    setPanelWidth((w) => {
      const next = clampWidth(w + (e.key === 'ArrowRight' ? step : -step));
      persistPanelWidth(next);
      return next;
    });
  }, []);

  // Inline grid-template — desktop använder bredden, mobil ignoreras
  // helt via CSS @media (max-width:800px) som kör vertikal stacking.
  const gridStyle = { gridTemplateColumns: `${panelWidth}px 6px 1fr` };

  // Persist UI-state vid varje förändring
  useEffect(() => {
    persist({
      search: searchQuery,
      statusFilter,
      dateFilter,
      currencyFilter,
      sortBy,
      sortDir,
    });
  }, [searchQuery, statusFilter, dateFilter, currencyFilter, sortBy, sortDir]);

  // FAS 8.5 — Persist Matchade-vy-state separat
  useEffect(() => {
    try {
      window.localStorage.setItem(LS_MODE_KEY, mode);
    } catch {
      /* ignore */
    }
  }, [mode]);
  useEffect(() => {
    try {
      window.localStorage.setItem(LS_MATCHED_PERIOD_KEY, matchedPeriod);
    } catch {
      /* ignore */
    }
  }, [matchedPeriod]);
  useEffect(() => {
    try {
      window.localStorage.setItem(LS_MATCHED_SEARCH_KEY, matchedSearch);
    } catch {
      /* ignore */
    }
  }, [matchedSearch]);

  const refreshMatched = useCallback(async () => {
    setMatchedLoading(true);
    try {
      const body = await api.matchedPairs({
        period: matchedPeriod,
        search: matchedSearch,
      });
      setMatchedData({
        pairs: body?.pairs || [],
        total: body?.total || 0,
        stats: body?.stats || {
          total_all_time: 0, this_week: 0, estimated_minutes_saved: 0,
        },
      });
    } catch (err) {
      const detail = err instanceof ApiError ? err.message : String(err);
      toast.show({
        kind: 'err',
        message: `${t.travelTinder.refreshFailed}: ${detail}`,
      });
    } finally {
      setMatchedLoading(false);
    }
  }, [matchedPeriod, matchedSearch, t.travelTinder.refreshFailed, toast]);

  // Hämta matchade-data när vi byter till matched-läge eller filter ändras.
  // Eftersom search/period är localStorage-persistenta är det rimligt att
  // ladda när användaren öppnar vyn även om den inte aktivt är vald.
  useEffect(() => {
    if (mode !== 'matched') return;
    refreshMatched();
  }, [mode, refreshMatched]);

  const refresh = useCallback(
    async ({ silent = false } = {}) => {
      if (!silent) setRefreshing(true);
      try {
        const body = await api.bezalaMatchSuggestionsAll();
        setData({
          missing_receipts: body?.missing_receipts || [],
          all_messages: body?.all_messages || [],
        });
        setLastRefresh(Date.now());
      } catch (err) {
        const msg = err instanceof ApiError ? err.message : String(err);
        toast.show({
          kind: 'err',
          message: `${t.travelTinder.refreshFailed}: ${msg}`,
        });
      } finally {
        setIsLoading(false);
        setRefreshing(false);
      }
    },
    [t.travelTinder.refreshFailed, toast],
  );

  useEffect(() => {
    refresh({ silent: true });
  }, [refresh]);

  useEffect(() => {
    const id = setInterval(() => refresh({ silent: true }), REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  // C14 — dölj rader som lokalt klarats men där refresh inte hunnit
  // hämta servern. inFlight-raden hålls kvar i listan med deemfas.
  const missingRows = useMemo(
    () =>
      data.missing_receipts.filter(
        (r) => !completedPaymentIds.has(r.missing_receipt.id),
      ),
    [data.missing_receipts, completedPaymentIds],
  );

  // Auto-välj första saknade kortbetalning om inget är valt
  useEffect(() => {
    if (selectedPaymentId == null && missingRows.length > 0) {
      setSelectedPaymentId(missingRows[0].missing_receipt.id);
    }
  }, [missingRows, selectedPaymentId]);

  const selected = useMemo(
    () =>
      missingRows.find((r) => r.missing_receipt.id === selectedPaymentId) ||
      null,
    [missingRows, selectedPaymentId],
  );

  const matchedCount = useMemo(
    () =>
      data.all_messages.filter((m) => m.coupled).length +
      completedPaymentIds.size,
    [data.all_messages, completedPaymentIds],
  );
  const totalCount = data.all_messages.length;

  const activeSuggestion = useMemo(() => {
    if (!selected) return null;
    const list = (selected.suggestions || []).filter(
      (s) => !skippedSuggestionIds.includes(s.message.id),
    );
    return list[0] || null;
  }, [selected, skippedSuggestionIds]);

  const onSelectPayment = useCallback((id) => {
    setSelectedPaymentId(id);
    setSkippedSuggestionIds([]);
    // FAS 5.16 — byte av korttrans rensar användarens kandidat-pick.
    setSelectedCandidateMessageId(null);
  }, []);

  // FAS 8.5c — fire-and-forget feedback. Får aldrig blockera kärnflödet:
  // alla fel sväljs i en .catch så match/skip-knapparna fortfarande
  // beter sig snabbt och deterministiskt även om backend är seg.
  const sendMatchFeedback = useCallback(
    ({ messageId, billLineId, result, aiScore, scoreBreakdown }) => {
      if (!messageId) return;
      api
        .feedbackMatchResult({
          messageId,
          billLineId,
          result,
          aiScore,
          scoreBreakdown,
        })
        .catch((err) => {
          // Tyst — feedback är best-effort. Console.warn för felsökning.
          // eslint-disable-next-line no-console
          console.warn('match-feedback failed:', err);
        });
    },
    [],
  );

  // FAS 5.16 — direkt couple-action utan bekräftelsemodal. Triggas av
  // explicit "Couple →"-knapp i Card A (AI) eller Card B (user-pick).
  // aiContext: { score, score_breakdown } när det kommer från AI-kortet,
  // null vid manuell koppling. Vid framgång rensas båda valen, nästa
  // korttrans väljs och listorna uppdateras.
  //
  // FAS 5.19 — matchingLockRef ger synkront re-entry-skydd. setMatching
  // är async, så en snabb dubbel-klick hinner annars fyra fram en andra
  // onClick innan React renderar disabled=true → två rader kopplas till
  // samma bill_line. Refen blockerar det synkront.
  const couple = useCallback(
    async (messageRow, missingReceiptId, aiContext = null) => {
      if (matching || !messageRow || !missingReceiptId) return;
      if (matchingLockRef.current) return;
      // C20 — if the bill_line the candidate-card captured at render time
      // no longer matches the user's current selection, a silent refresh
      // shuffled the list under them. Aborting prevents the Lovable→Finnair
      // bug (receipt coupled to a stranger bank row).
      if (
        selectedPaymentId != null &&
        selectedPaymentId !== missingReceiptId
      ) {
        toast.show({
          kind: 'err',
          message: t.travelTinder.selectionChangedAbort,
        });
        return;
      }
      matchingLockRef.current = true;
      setMatching(true);
      // C14 — visa deemfas + "kopplar…"-indikator på raden direkt så
      // användaren ser att klicket registrerats även om POST tar tid.
      setInFlightPaymentId(missingReceiptId);

      // C14 — auto-välj nästa korttrans direkt (innan POST returnerar)
      // så användaren kan fortsätta utan att vänta. Söker i rå-listan
      // för att hoppa över rader som redan är completed eller in-flight.
      const rawMissing = data.missing_receipts;
      const idx = rawMissing.findIndex(
        (r) => r.missing_receipt.id === missingReceiptId,
      );
      const findCandidate = (predicate) =>
        rawMissing.find(
          (r) =>
            r.missing_receipt.id !== missingReceiptId &&
            !completedPaymentIds.has(r.missing_receipt.id) &&
            predicate(r),
        );
      const nextRow =
        findCandidate((_r) => rawMissing.indexOf(_r) > idx) ||
        findCandidate(() => true) ||
        null;

      // Rensa kandidatpanelen direkt (modal är redan borta i FAS 5.16).
      setSkippedSuggestionIds([]);
      setSelectedCandidateMessageId(null);
      setSelectedPaymentId(nextRow ? nextRow.missing_receipt.id : null);

      const markCompletedAndRefresh = async () => {
        setCompletedPaymentIds((prev) => {
          const next = new Set(prev);
          next.add(missingReceiptId);
          return next;
        });
        try {
          await refresh({ silent: true });
        } finally {
          // Ta bort optimistic-flaggan när serverstate hunnit in så
          // raden inte längre styrs av lokala overlays.
          setCompletedPaymentIds((prev) => {
            if (!prev.has(missingReceiptId)) return prev;
            const next = new Set(prev);
            next.delete(missingReceiptId);
            return next;
          });
        }
      };

      try {
        await api.matchToBezala(messageRow.id, missingReceiptId);
        sendMatchFeedback({
          messageId: messageRow.message_id,
          billLineId: missingReceiptId,
          result: 'matched',
          aiScore: aiContext?.score ?? null,
          scoreBreakdown: aiContext?.score_breakdown || null,
        });
        toast.show({
          kind: 'ok',
          message: t.travelTinder.matchSuccess.replace(
            '{vendor}',
            messageRow.vendor || messageRow.file_name || '',
          ),
        });
        await markCompletedAndRefresh();
      } catch (err) {
        const status = err instanceof ApiError ? err.status : null;
        if (status === 409) {
          // C14 / memory #18 — backend säger att raden redan är
          // kopplad (concurrent click eller stale UI). Behandla som
          // success: dölj raden, refresha för att synka.
          toast.show({
            kind: 'ok',
            message: t.travelTinder.alreadyCoupled,
          });
          await markCompletedAndRefresh();
        } else {
          const detail = err instanceof ApiError ? err.message : String(err);
          toast.show({
            kind: 'err',
            message: `${t.travelTinder.matchFailed}: ${detail}`,
          });
          // Återställ urvalet — användaren ska kunna försöka igen.
          setSelectedPaymentId(missingReceiptId);
        }
      } finally {
        setMatching(false);
        setInFlightPaymentId(null);
        matchingLockRef.current = false;
      }
    },
    [
      matching,
      selectedPaymentId,
      data.missing_receipts,
      completedPaymentIds,
      refresh,
      sendMatchFeedback,
      t.travelTinder.matchSuccess,
      t.travelTinder.matchFailed,
      t.travelTinder.alreadyCoupled,
      t.travelTinder.selectionChangedAbort,
      toast,
    ],
  );

  // FAS 5.16 — klick på rad i Other Receipts gör inte längre direkt
  // koppling. Den sätter raden som kandidat (Card B). Klick på samma
  // rad igen deselectar. Coupled-rader öppnar drawer som tidigare.
  const onClickReceipt = useCallback(
    (msg) => {
      if (msg.coupled || !selected) {
        openDrawer(msg, 'gmail');
        return;
      }
      setSelectedCandidateMessageId((prev) =>
        prev === msg.id ? null : msg.id,
      );
    },
    [openDrawer, selected],
  );

  const onClearCandidate = useCallback(() => {
    setSelectedCandidateMessageId(null);
  }, []);

  const onShowPdfPreview = useCallback((msg) => {
    setPdfPreviewMessage(msg);
  }, []);

  // Slå upp användarens kandidat-meddelande i all_messages. Ignoreras
  // om det råkar vara coupled (skuggad rad). Auto-clear av icke-giltigt
  // val undviks medvetet — vi visar bara kortet om det finns och är okopplat.
  const userPickMessage = useMemo(() => {
    if (selectedCandidateMessageId == null) return null;
    const msg = data.all_messages.find(
      (m) => m.id === selectedCandidateMessageId,
    );
    if (!msg || msg.coupled) return null;
    return msg;
  }, [data.all_messages, selectedCandidateMessageId]);

  return (
    <div className="travel-tinder" data-testid="travel-tinder">
      <header className="travel-tinder__head">
        <h1 className="travel-tinder__title">{t.travelTinder.title}</h1>
        <div className="travel-tinder__head-meta">
          <span className="muted mono" data-testid="last-refresh">
            {t.travelTinder.lastRefresh.replace(
              '{when}',
              formatRelative(lastRefresh, t),
            )}
          </span>
          <button
            type="button"
            className="btn ghost"
            onClick={() => refresh()}
            disabled={refreshing}
            data-testid="tt-refresh"
            aria-label={t.travelTinder.refresh}
          >
            <IconRefresh className="icon sm" />
          </button>
        </div>
      </header>

      <div className="travel-tinder__grid" style={gridStyle}>
        <MissingPaymentsList
          rows={missingRows}
          selectedId={selectedPaymentId}
          onSelect={onSelectPayment}
          matchedCount={matchedCount}
          totalCount={totalCount}
          isLoading={isLoading}
          mode={mode}
          onModeChange={setMode}
          unmatchedCount={missingRows.length}
          matchedTotalCount={matchedData.stats?.total_all_time ?? 0}
          inFlightPaymentId={inFlightPaymentId}
        />

        <div
          className="tt-splitter"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize panels"
          aria-valuenow={panelWidth}
          aria-valuemin={PANEL_WIDTH_MIN}
          aria-valuemax={PANEL_WIDTH_MAX}
          tabIndex={0}
          onMouseDown={onSplitterMouseDown}
          onKeyDown={onSplitterKeyDown}
          data-testid="tt-splitter"
        >
          <span className="tt-splitter__grip" aria-hidden="true" />
        </div>

        <div className="travel-tinder__right">
          {mode === 'matched' ? (
            <MatchedPairsList
              data={matchedData}
              isLoading={matchedLoading}
              search={matchedSearch}
              setSearch={setMatchedSearch}
              period={matchedPeriod}
              setPeriod={setMatchedPeriod}
              onOpenDrawer={(pair) => openDrawer(
                {
                  // Drawer förväntar sig samma shape som ProcessedMessage —
                  // vi har bara delar i pair.receipt så vi rebygger ett
                  // minimalt objekt. Drawer-tabbarna laddar resten via API.
                  id: pair.id,
                  message_id: pair.message_id,
                  vendor: pair.receipt?.vendor,
                  file_name: pair.receipt?.file_name,
                  amount: pair.receipt?.amount,
                  currency: pair.receipt?.currency,
                  receipt_date: pair.receipt?.receipt_date,
                  drive_file_id: pair.receipt?.drive_file_id,
                  drive_link: pair.receipt?.drive_link,
                  subject: pair.receipt?.subject,
                  sender: pair.receipt?.sender,
                  bezala_transaction_id: pair.bezala_transaction_id,
                  bezala_upload_status: 'success',
                  status: 'saved',
                },
                'gmail',
              )}
              onChanged={() => {
                // Refresha både listorna efter unmatch
                refreshMatched();
                refresh({ silent: true });
              }}
            />
          ) : null}
          {mode !== 'matched' ? (
          <OtherReceiptsList
            allMessages={data.all_messages}
            selected={selected}
            activeSuggestion={activeSuggestion}
            selectedCandidateMessageId={selectedCandidateMessageId}
            onClickReceipt={onClickReceipt}
            onShowPdfPreview={onShowPdfPreview}
            search={searchQuery}
            setSearch={setSearchQuery}
            statusFilter={statusFilter}
            setStatusFilter={setStatusFilter}
            dateFilter={dateFilter}
            setDateFilter={setDateFilter}
            currencyFilter={currencyFilter}
            setCurrencyFilter={setCurrencyFilter}
            sortBy={sortBy}
            setSortBy={setSortBy}
            sortDir={sortDir}
            setSortDir={setSortDir}
            tinderCard={
              selected ? (
                <MatchCandidates
                  aiSuggestion={activeSuggestion}
                  userPickMessage={userPickMessage}
                  paymentId={selected.missing_receipt.id}
                  onClearUserPick={onClearCandidate}
                  onCouple={couple}
                  onOpenDrawer={(messageRow) =>
                    openDrawer(messageRow, 'gmail')
                  }
                  coupling={matching}
                />
              ) : null
            }
            uploadCard={
              <UploadCard
                payment={selected?.missing_receipt}
                onUploaded={(result) => {
                  const coupled = result?.coupling?.ok === true;
                  const vendor =
                    result?.message?.vendor || result?.message?.file_name || '';
                  if (coupled) {
                    toast.show({
                      kind: 'ok',
                      message: t.travelTinder.upload.successCoupled.replace(
                        '{vendor}',
                        vendor,
                      ),
                    });
                    // Optimistic-flagga + advance till nästa rad så
                    // upplevelsen blir samma som vid en vanlig Couple.
                    if (selected?.missing_receipt?.id != null) {
                      const completedId = selected.missing_receipt.id;
                      setCompletedPaymentIds((prev) => {
                        const next = new Set(prev);
                        next.add(completedId);
                        return next;
                      });
                      const rawMissing = data.missing_receipts;
                      const idx = rawMissing.findIndex(
                        (r) => r.missing_receipt.id === completedId,
                      );
                      const nextRow =
                        rawMissing
                          .slice(idx + 1)
                          .find(
                            (r) =>
                              !completedPaymentIds.has(r.missing_receipt.id),
                          ) ||
                        rawMissing.find(
                          (r) =>
                            r.missing_receipt.id !== completedId &&
                            !completedPaymentIds.has(r.missing_receipt.id),
                        ) ||
                        null;
                      setSelectedPaymentId(
                        nextRow ? nextRow.missing_receipt.id : null,
                      );
                    }
                  } else {
                    toast.show({
                      kind: 'ok',
                      message: t.travelTinder.upload.success,
                    });
                  }
                  refresh({ silent: true });
                }}
              />
            }
            isLoading={isLoading}
          />
          ) : null}
        </div>
      </div>

      {pdfPreviewMessage ? (
        <PdfPreviewLightbox
          message={pdfPreviewMessage}
          onClose={() => setPdfPreviewMessage(null)}
        />
      ) : null}
    </div>
  );
}
