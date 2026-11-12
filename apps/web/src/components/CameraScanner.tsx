"use client";

import jsQR from "jsqr";
import { useEffect, useRef, useState } from "react";

type Detector = { detect: (source: CanvasImageSource) => Promise<{ rawValue: string }[]> };

/** The browser's built-in QR reader where there is one (faster, and it runs off the main thread). */
function nativeDetector(): Detector | null {
  const Native = (
    globalThis as unknown as { BarcodeDetector?: new (options: { formats: string[] }) => Detector }
  ).BarcodeDetector;
  try {
    return Native ? new Native({ formats: ["qr_code"] }) : null;
  } catch {
    return null;
  }
}

/**
 * Point the camera at a ticket. Calls `onCode` with what each QR code
 * contains: once per code, and again only after it has been out of view, so
 * a ticket held in front of the lens is scanned once and not thirty times a
 * second.
 */
export function CameraScanner({ onCode, paused }: { onCode: (code: string) => void; paused: boolean }) {
  const video = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const handler = useRef(onCode);
  const pausedRef = useRef(paused);
  useEffect(() => {
    handler.current = onCode;
    pausedRef.current = paused;
  });

  useEffect(() => {
    let stream: MediaStream | null = null;
    let frame = 0;
    let stopped = false;
    let last = "";
    let lastSeen = 0;
    const detector = nativeDetector();
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d", { willReadFrequently: true });

    const read = async (element: HTMLVideoElement): Promise<string | null> => {
      if (detector) {
        try {
          const found = await detector.detect(element);
          return found[0]?.rawValue ?? null;
        } catch {
          // Fall through to the JavaScript reader for this frame.
        }
      }
      if (!context) return null;
      // Tickets are dense codes: read at the camera's resolution, capped so
      // a 4K camera does not make every frame take a second.
      const scale = Math.min(1, 960 / element.videoWidth);
      canvas.width = Math.round(element.videoWidth * scale);
      canvas.height = Math.round(element.videoHeight * scale);
      context.drawImage(element, 0, 0, canvas.width, canvas.height);
      const image = context.getImageData(0, 0, canvas.width, canvas.height);
      return jsQR(image.data, image.width, image.height)?.data || null;
    };

    const tick = async () => {
      if (stopped) return;
      const element = video.current;
      if (element && element.readyState >= 2 && element.videoWidth > 0 && !pausedRef.current) {
        const code = await read(element);
        const now = performance.now();
        if (code) {
          // New code, or the same one back after two seconds out of view.
          if (code !== last || now - lastSeen > 2000) handler.current(code);
          last = code;
          lastSeen = now;
        }
      }
      // About eight looks a second: plenty for a person presenting a ticket.
      frame = window.setTimeout(tick, 120);
    };

    (async () => {
      if (!navigator.mediaDevices?.getUserMedia) {
        setError("This browser cannot use a camera here. Paste the ticket code instead.");
        return;
      }
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 } },
          audio: false,
        });
        if (stopped) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        if (video.current) {
          video.current.srcObject = stream;
          await video.current.play();
          setReady(true);
          tick();
        }
      } catch (reason) {
        const name = reason instanceof DOMException ? reason.name : "";
        setError(
          name === "NotAllowedError"
            ? "Camera access was refused. Allow it in the browser's site settings, or paste the code."
            : name === "NotFoundError"
              ? "No camera was found. Paste the ticket code instead."
              : "The camera could not be started. Paste the ticket code instead.",
        );
      }
    })();

    return () => {
      stopped = true;
      window.clearTimeout(frame);
      stream?.getTracks().forEach((track) => track.stop());
    };
  }, []);

  if (error) {
    return (
      <p role="alert" className="rounded-lg border border-warn/40 bg-warn/10 px-3.5 py-2.5 text-sm text-warn">
        {error}
      </p>
    );
  }
  return (
    <div className="relative mx-auto aspect-[4/3] max-h-[22rem] overflow-hidden rounded-xl border border-line bg-black">
      <video ref={video} muted playsInline className="size-full object-cover" aria-label="Camera view" />
      {/* A frame to aim with. The whole picture is read, not just the box. */}
      <div aria-hidden className="pointer-events-none absolute inset-0 grid place-items-center">
        <div
          className={`size-[58%] rounded-2xl border-2 transition-colors ${
            paused ? "border-muted/60" : "border-primary shadow-[0_0_0_100vmax_rgb(0_0_0/0.35)]"
          }`}
        />
      </div>
      {!ready && (
        <p className="absolute inset-0 grid place-items-center text-sm text-muted">Starting the camera</p>
      )}
    </div>
  );
}
