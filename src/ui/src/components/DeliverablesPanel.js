import { memo } from 'react';
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Button from "@cloudscape-design/components/button";

// A file size for display: "512 B", "20 KB", "1.2 MB".
export function formatBytes(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = n / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const shown = value >= 10 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${shown} ${units[unit]}`;
}

/**
 * "Deliverables" (#957): the files of the job's deliverables/ folder (#955),
 * as GET /assessments/{job_id}/deliverables lists them (the folder's own
 * definition in src/report/deliverables.py, plus its index.md), each with a
 * download link to GET /assessments/{job_id}/deliverables/{filename}.
 *
 * Callers pass the listing (`files`, `loading`, `error`), the API base URL
 * and `t` (the page's own `useTranslation` function), like QueryRoutingTable.
 */
const DeliverablesPanel = memo(({ files, loading, error, baseUrl, t }) => {
  let body;
  if (loading) {
    body = <StatusIndicator type="loading">{t('analysis-results.deliverables.loading')}</StatusIndicator>;
  } else if (error) {
    body = <StatusIndicator type="error">{t('analysis-results.deliverables.error', { error })}</StatusIndicator>;
  } else if (!files || files.length === 0) {
    body = <Box color="text-body-secondary">{t('analysis-results.deliverables.empty')}</Box>;
  } else {
    body = (
      <SpaceBetween size="m">
        {files.map(file => (
          <div key={file.filename} data-deliverable={file.filename}>
            <ColumnLayout columns={3} variant="text-grid">
              <div>
                <Box fontWeight="bold">{file.title}</Box>
                <Box fontSize="body-s" color="text-body-secondary"><code>{file.filename}</code> · {formatBytes(file.size_bytes)}</Box>
              </div>
              <Box color="text-body-secondary">{file.audience}</Box>
              <Box textAlign="right">
                <Button
                  iconName="download"
                  href={`${baseUrl}${file.path}`}
                  download={file.filename}
                  ariaLabel={t('analysis-results.deliverables.download-label', { filename: file.filename })}
                >
                  {t('analysis-results.deliverables.download')}
                </Button>
              </Box>
            </ColumnLayout>
          </div>
        ))}
      </SpaceBetween>
    );
  }
  return (
    <div id="deliverables-section">
      <Container
        header={(
          <Header variant="h2" description={t('analysis-results.deliverables.description')}>
            {t('analysis-results.deliverables.title')}
          </Header>
        )}
      >
        {body}
      </Container>
    </div>
  );
});

export default DeliverablesPanel;
