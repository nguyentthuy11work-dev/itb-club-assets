export function formatTicketActionError(error) {
  const message = error?.message || 'Thao tác thất bại, thử lại.';
  const code = error?.payload?.error?.code;
  return code ? `${message} (${code})` : message;
}
