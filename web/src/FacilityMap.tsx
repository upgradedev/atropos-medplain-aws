import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { CaseSummary, Facility } from "./types";

export default function FacilityMap({
  facilities,
  cases,
  select,
}: {
  facilities: Facility[];
  cases: CaseSummary[];
  select: (id: string) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [tileError, setTileError] = useState(false);
  useEffect(() => {
    if (!host.current) return;
    const map = L.map(host.current, { scrollWheelZoom: false }).setView(
      [38.7, 23.2],
      6,
    );
    const tiles = L.tileLayer(
      "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      {
        maxZoom: 18,
        attribution:
          '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      },
    ).addTo(map);
    tiles.on("tileerror", () => setTileError(true));
    facilities.forEach((facility) => {
      const items = cases.filter((item) => item.facility_id === facility.id);
      const popup = document.createElement("div");
      const heading = document.createElement("strong");
      heading.textContent = facility.name;
      popup.append(heading);
      const description = document.createElement("p");
      description.textContent = `${facility.city} · ${items.length} cases in current session. Synthetic operations desk, not patient locations.`;
      popup.append(description);
      const button = document.createElement("button");
      button.textContent = "Open desk cases";
      button.className = "map-popup-button";
      button.addEventListener("click", () => select(facility.id));
      popup.append(button);
      L.circleMarker([facility.lat, facility.lon], {
        radius: 12 + Math.min(items.length, 8),
        color: "#78e2c1",
        fillColor: "#183b3b",
        fillOpacity: 0.9,
        weight: 2,
      })
        .addTo(map)
        .bindPopup(popup)
        .bindTooltip(`${facility.city}: ${items.length} cases`);
    });
    const observer = new ResizeObserver(() => map.invalidateSize());
    observer.observe(host.current);
    return () => {
      observer.disconnect();
      map.remove();
    };
  }, [facilities, cases, select]);
  return (
    <>
      <div
        className="leaflet-host"
        ref={host}
        aria-label="Synthetic facility map of Greece"
        data-testid="facility-map"
      />
      {tileError ? (
        <p role="alert" className="notice">
          The public basemap is unavailable. Desk markers and the facility list
          remain usable. No location service credentials are required.
        </p>
      ) : null}
      <p className="map-caption">
        Public OpenStreetMap basemap. Pan and zoom the map. Markers are
        synthetic operations desks in Athens, Thessaloniki and Patras. They do
        not represent hospitals, patients or live care coverage.
      </p>
    </>
  );
}
