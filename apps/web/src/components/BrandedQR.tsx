"use client";

import QRCode from "qrcode";
import { useId, useMemo } from "react";

const INK = "#09090a";
const PAPER = "#fffaf0";
/** Dark enough for a scanner to read as "black", warm enough to be ours. */
const EMBER = "#9a3412";

/**
 * A QR code in lobbyd's own hand: soft-cornered modules on warm paper,
 * ticket-stub corner markers with an ember centre, and the mark in the middle.
 *
 * It must still scan first time at a door, so the rules of the format are
 * kept: dark on light, a quiet margin, corner markers in their 1:1:3:1:1
 * proportions, and error correction high enough (Q, a quarter of the code)
 * to pay for the modules the logo covers.
 */
export function BrandedQR({ value, label }: { value: string; label: string }) {
  const id = useId();
  const code = useMemo(() => {
    try {
      return QRCode.create(value, { errorCorrectionLevel: "Q" }).modules;
    } catch {
      return null;
    }
  }, [value]);
  if (!code) return <span className="text-xs text-bg">QR</span>;

  const n = code.size;
  const margin = 2;
  const full = n + margin * 2;
  // The logo's clearing: an odd number of modules, about a fifth of the width.
  const hole = Math.max(7, Math.floor(n * 0.2) | 1);
  const from = (n - hole) / 2;
  const inHole = (x: number, y: number) => x >= from && x < from + hole && y >= from && y < from + hole;
  const inEye = (x: number, y: number) =>
    (x < 7 && y < 7) || (x >= n - 7 && y < 7) || (x < 7 && y >= n - 7);

  let dots = "";
  for (let y = 0; y < n; y++) {
    for (let x = 0; x < n; x++) {
      if (!code.data[y * n + x] || inEye(x, y) || inHole(x, y)) continue;
      // A rounded square filling its cell. Neighbours touch, so runs of
      // modules read as solid bars to a scanner and as beads to a person.
      dots += `M${x + 0.34} ${y}h.32a.34.34 0 0 1 .34.34v.32a.34.34 0 0 1-.34.34h-.32a.34.34 0 0 1-.34-.34v-.32a.34.34 0 0 1 .34-.34z`;
    }
  }
  const eyes: [number, number][] = [
    [0, 0],
    [n - 7, 0],
    [0, n - 7],
  ];
  const logoSize = hole - 1.4;
  const logoAt = from + 0.7;

  return (
    <svg
      viewBox={`${-margin} ${-margin} ${full} ${full}`}
      role="img"
      aria-label={label}
      className="size-full"
      shapeRendering="geometricPrecision"
    >
      <defs>
        <linearGradient id={`${id}g`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#ffd166" />
          <stop offset="1" stopColor="#ff8a00" />
        </linearGradient>
      </defs>
      <rect x={-margin} y={-margin} width={full} height={full} rx="2.4" fill={PAPER} />
      <path d={dots} fill={INK} />
      {eyes.map(([x, y]) => (
        <g key={`${x}-${y}`}>
          {/* Outer ring: 7 wide, 1 thick. */}
          <path
            fill={INK}
            fillRule="evenodd"
            d={`M${x + 2.2} ${y}h2.6a2.2 2.2 0 0 1 2.2 2.2v2.6a2.2 2.2 0 0 1-2.2 2.2h-2.6a2.2 2.2 0 0 1-2.2-2.2v-2.6a2.2 2.2 0 0 1 2.2-2.2zM${x + 2.2} ${y + 1}a1.2 1.2 0 0 0-1.2 1.2v2.6a1.2 1.2 0 0 0 1.2 1.2h2.6a1.2 1.2 0 0 0 1.2-1.2v-2.6a1.2 1.2 0 0 0-1.2-1.2z`}
          />
          <rect x={x + 2} y={y + 2} width="3" height="3" rx="0.95" fill={EMBER} />
        </g>
      ))}
      {/* The mark, scaled from its 64-unit drawing into the clearing. */}
      <g transform={`translate(${logoAt} ${logoAt}) scale(${logoSize / 64})`}>
        <path
          fill={`url(#${id}g)`}
          d="M18 4h28a14 14 0 0 1 14 14v7a7 7 0 0 0 0 14v7a14 14 0 0 1-14 14H18A14 14 0 0 1 4 46v-7a7 7 0 0 0 0-14v-7A14 14 0 0 1 18 4z"
        />
        <path
          fill={INK}
          fillRule="evenodd"
          d="M40.5 13a4 4 0 0 1 4 4v29a7.5 7.5 0 0 1-7.5 7.5H24A9.5 9.5 0 0 1 14.5 44v-7A9.5 9.5 0 0 1 24 27.5h12.5V17a4 4 0 0 1 4-4zM24.5 34.5a3 3 0 0 0-3 3v6a3 3 0 0 0 3 3h9a3 3 0 0 0 3-3v-9z"
        />
        <rect x="25" y="36.75" width="7.5" height="7.5" rx="2" fill="#fff" />
      </g>
    </svg>
  );
}
