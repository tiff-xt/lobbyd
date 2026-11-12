import { useId } from "react";

/**
 * The lobbyd mark: a ticket (the notched square) holding the "d" of the
 * name, whose bowl is a seat with one lit square in it: the one that is yours.
 * Kept in step with src/app/icon.svg, the favicon.
 */
export function Logo({ size = 28 }: { size?: number }) {
  // Ids must be unique per instance, or two logos on a page share a gradient.
  const id = useId();
  return (
    <svg viewBox="0 0 64 64" width={size} height={size} className="logo-mark" aria-hidden>
      <defs>
        <linearGradient id={`${id}g`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#ffd166" />
          <stop offset="1" stopColor="#ff8a00" />
        </linearGradient>
        <filter id={`${id}f`} x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="2.2" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>
      <path
        fill={`url(#${id}g)`}
        d="M18 4h28a14 14 0 0 1 14 14v7a7 7 0 0 0 0 14v7a14 14 0 0 1-14 14H18A14 14 0 0 1 4 46v-7a7 7 0 0 0 0-14v-7A14 14 0 0 1 18 4z"
      />
      <path
        fill="#09090a"
        fillRule="evenodd"
        d="M40.5 13a4 4 0 0 1 4 4v29a7.5 7.5 0 0 1-7.5 7.5H24A9.5 9.5 0 0 1 14.5 44v-7A9.5 9.5 0 0 1 24 27.5h12.5V17a4 4 0 0 1 4-4zM24.5 34.5a3 3 0 0 0-3 3v6a3 3 0 0 0 3 3h9a3 3 0 0 0 3-3v-9z"
      />
      <rect
        className="logo-seat"
        x="25"
        y="36.75"
        width="7.5"
        height="7.5"
        rx="2"
        fill="#fff"
        filter={`url(#${id}f)`}
      />
    </svg>
  );
}
