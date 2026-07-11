type PaginationProps = {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
};

export function Pagination({ page, pageSize, total, onPageChange }: PaginationProps) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="pagination">
      <span>
        第 {page} / {pageCount} 页，共 {total} 条
      </span>
      <div className="pagination-actions">
        <button className="secondary-button" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>
          上一页
        </button>
        <button className="secondary-button" disabled={page >= pageCount} onClick={() => onPageChange(page + 1)}>
          下一页
        </button>
      </div>
    </div>
  );
}
