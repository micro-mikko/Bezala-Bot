import { useCallback, useRef, useState } from 'react';
import { useI18n } from '../../i18n/useI18n.jsx';
import { api, ApiError } from '../../api/client.js';

/* C33 / FAS 7a — manuell kvittouppladdning för företagskort.
 * Ersätter den tidigare disabled "Coming in FAS 7"-platshållaren med ett
 * funktionellt kort: drag-drop + filväljare, betalningsmetod-toggle
 * (företagskort aktivt, privat kort stubbat tills FAS 7b/Netvisor),
 * valfritt kommentar-fält, och upload-knapp som anropar /api/messages/upload.
 *
 * Om `payment` är satt (en bill_line) kopplas det uppladdade kvittot
 * direkt till den kortraden (samma flöde som Couple). Vid framgång
 * triggas onUploaded så TravelTinder kan refresha listorna och välja
 * nästa rad — samma UX som vanlig couple-action. */

const ACCEPT = '.pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png';
const MAX_BYTES = 50 * 1024 * 1024;

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function UploadCard({ payment, onUploaded }) {
  const { t } = useI18n();
  const tu = t.travelTinder.upload;
  const inputRef = useRef(null);

  const [file, setFile] = useState(null);
  const [paymentMethod, setPaymentMethod] = useState('company_card');
  const [comment, setComment] = useState('');
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState(null);
  const [dragActive, setDragActive] = useState(false);

  const billLineId = payment?.id ?? null;
  const title = payment ? tu.titleForSelected : tu.title;
  const uploadLabel = billLineId != null ? tu.uploadBtn : tu.uploadBtnNoCouple;

  const onPickFile = useCallback((f) => {
    if (!f) {
      setFile(null);
      return;
    }
    if (f.size > MAX_BYTES) {
      setError(tu.errorTooLarge);
      setFile(null);
      return;
    }
    const lower = (f.name || '').toLowerCase();
    const isAllowedExt = ['.pdf', '.jpg', '.jpeg', '.png'].some((ext) =>
      lower.endsWith(ext),
    );
    const isAllowedMime = [
      'application/pdf',
      'image/jpeg',
      'image/jpg',
      'image/png',
    ].includes((f.type || '').toLowerCase());
    if (!isAllowedExt && !isAllowedMime) {
      setError(tu.errorWrongType);
      setFile(null);
      return;
    }
    setError(null);
    setFile(f);
  }, [tu.errorTooLarge, tu.errorWrongType]);

  const onChooseClick = () => {
    if (uploading) return;
    inputRef.current?.click();
  };

  const onInputChange = (e) => {
    const f = e.target.files?.[0];
    onPickFile(f);
    // Reset input så samma filnamn kan väljas igen efter en upload
    e.target.value = '';
  };

  const onDragOver = (e) => {
    if (uploading) return;
    e.preventDefault();
    setDragActive(true);
  };
  const onDragLeave = (e) => {
    e.preventDefault();
    setDragActive(false);
  };
  const onDrop = (e) => {
    e.preventDefault();
    setDragActive(false);
    if (uploading) return;
    const f = e.dataTransfer?.files?.[0];
    onPickFile(f);
  };

  const onUpload = async () => {
    if (!file || uploading) return;
    setUploading(true);
    setError(null);
    try {
      const result = await api.uploadManualReceipt({
        file,
        paymentMethod,
        comment,
        billLineId,
      });
      setFile(null);
      setComment('');
      if (typeof onUploaded === 'function') {
        onUploaded(result);
      }
    } catch (err) {
      const detail =
        err instanceof ApiError ? err.message : String(err);
      setError(`${tu.errorGeneric}: ${detail}`);
    } finally {
      setUploading(false);
    }
  };

  return (
    <div
      className={`tt-upload-card-v2 ${dragActive ? 'is-drag-active' : ''}`}
      data-testid="tt-upload-card"
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      <div className="tt-upload-card-v2__head">
        <span className="tt-upload-card-v2__icon" aria-hidden="true">📷</span>
        <h4 className="tt-upload-card-v2__title">{title}</h4>
      </div>

      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        onChange={onInputChange}
        className="tt-upload-card-v2__input"
        data-testid="tt-upload-file-input"
        aria-label={tu.chooseFile}
      />

      {file ? (
        <div
          className="tt-upload-card-v2__file mono"
          data-testid="tt-upload-file-row"
        >
          <span className="tt-upload-card-v2__file-name">{file.name}</span>
          <span className="muted">{formatSize(file.size)}</span>
          <button
            type="button"
            className="btn ghost sm"
            onClick={() => setFile(null)}
            disabled={uploading}
            data-testid="tt-upload-file-remove"
            aria-label={tu.removeFile}
          >
            ×
          </button>
        </div>
      ) : (
        <button
          type="button"
          className="tt-upload-card-v2__drop"
          onClick={onChooseClick}
          disabled={uploading}
          data-testid="tt-upload-choose"
        >
          <span>{tu.dropHint}</span>
          <span className="muted mono">{tu.accept}</span>
        </button>
      )}

      <div className="tt-upload-card-v2__method">
        <span className="tt-upload-card-v2__method-label muted">
          {tu.methodLabel}
        </span>
        <div className="tt-upload-card-v2__method-toggle" role="radiogroup">
          <button
            type="button"
            role="radio"
            aria-checked={paymentMethod === 'company_card'}
            className={`tt-upload-card-v2__method-btn ${
              paymentMethod === 'company_card' ? 'is-active' : ''
            }`}
            onClick={() => setPaymentMethod('company_card')}
            disabled={uploading}
            data-testid="tt-upload-method-company"
          >
            {tu.companyCard}
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={false}
            className="tt-upload-card-v2__method-btn is-stub"
            disabled
            title={tu.privateCardComingSoon}
            data-testid="tt-upload-method-private"
          >
            {tu.privateCard}
            <span className="tt-upload-card-v2__stub-badge mono">
              {tu.privateCardComingSoon}
            </span>
          </button>
        </div>
      </div>

      <label className="tt-upload-card-v2__comment">
        <span className="muted">{tu.commentLabel}</span>
        <input
          type="text"
          className="tt-upload-card-v2__comment-input"
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder={tu.commentPlaceholder}
          disabled={uploading}
          data-testid="tt-upload-comment"
        />
      </label>

      <button
        type="button"
        className="btn primary tt-upload-card-v2__submit"
        onClick={onUpload}
        disabled={!file || uploading}
        data-testid="tt-upload-submit"
      >
        {uploading ? tu.uploading : uploadLabel}
      </button>

      {error ? (
        <p className="tt-upload-card-v2__error" data-testid="tt-upload-error">
          {error}
        </p>
      ) : null}
    </div>
  );
}
