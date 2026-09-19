# Foundry model inventory

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

People managing Azure subscriptions who need to find models, deployment types
and remaining quota across their estate, without opening each region manually.

## Product Purpose

A local inventory workbench: collect the read-only Foundry catalog and quota,
filter it precisely, and compare dated snapshots.

## Operating Context

The repository and application run locally on Windows. Azure CLI provides the
user's existing authentication. Daily collection must work independently of
whether the dashboard is open. The browser connects only to localhost.

## Capabilities and Constraints

- Filter subscriptions, regions, provider/model family, model, version,
  deployment geography, billing/capacity type and quota availability.
- Treat a model/version pair as one selectable catalog choice; allow broader
  model-name selection and family-to-version drill-down.
- Support multiple selections within categorical filters, readable bar charts
  and a system-aware dark/light appearance without adding frontend dependencies.
- Preserve historical snapshots instead of replacing yesterday's data.
- Reuse the existing PowerShell collectors; keep their standalone usage intact.
- Keep the application lightweight, without a cloud service or database server.
- Never equate catalog availability or remaining quota with guaranteed capacity.
- Keep tenant configuration, databases, reports and logs out of the public repo.
- Surface stale data, authentication failures, partial scans and unknown quota.

## Product Principles

- Precise scope and units before attractive totals.
- An unsuccessful scan must not silently replace a healthy current view.
- Give the user the filtered data, not just a chart.
- Read-only Azure access; local collection controls are explicit.

## Accessibility and Inclusion

Use semantic tables and forms, keyboard-accessible controls, visible focus,
non-color status labels, and layouts that work on narrower screens.

## Implementation Assumptions

SQLite is the local snapshot store. The initial morning schedule is 07:00 in the
workstation's local timezone, editable by the user. The first version is a
single-user local application, not a remotely hosted multi-user service.
