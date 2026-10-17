import type { ComponentPropsWithoutRef } from 'react'

/** 宽表仅在自身区域滚动，长编号不会撑开页面或被外层卡片裁切。 */
export function EvaluationTable({
  className = 'eval-table',
  ...props
}: ComponentPropsWithoutRef<'table'>) {
  return (
    <div className="eval-table-viewport" role="region" aria-label="数据表，可横向滚动" tabIndex={0}>
      <table className={className} {...props} />
    </div>
  )
}
