import { useEffect, useRef } from "react";
import L from "leaflet";
import type { FeatureCollection } from "geojson";
import "leaflet/dist/leaflet.css";
import regionalCountries from "./data/greece-region.geojson.json";
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
  useEffect(() => {
    if (!host.current) return;
    const map = L.map(host.current, {
      scrollWheelZoom: false,
      minZoom: 5,
      maxZoom: 8,
      maxBounds: [
        [33.5, 17.5],
        [44.5, 31.5],
      ],
      maxBoundsViscosity: 1,
    }).setView([38.7, 23.2], 6);
    // Bundled country geometry makes the entire basemap independent of tile APIs.
    L.geoJSON(regionalCountries as FeatureCollection, {
      interactive: false,
      style: (feature) => ({
        className: "geographic-country",
        color: "#638198",
        weight: 1.1,
        fillColor: feature?.properties.code === "GRC" ? "#2c5058" : "#263748",
        fillOpacity: 1,
      }),
      onEachFeature: (feature, layer) => {
        layer.on("add", () => {
          if (layer instanceof L.Path) {
            layer
              .getElement()
              ?.setAttribute("data-country", feature.properties.code);
          }
        });
      },
    }).addTo(map);
    map.attributionControl.addAttribution(
      'Made with <a href="https://www.naturalearthdata.com/about/terms-of-use/">Natural Earth</a> · Public domain',
    );
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
      const marker = L.circleMarker([facility.lat, facility.lon], {
        className: "facility-marker",
        radius: 12 + Math.min(items.length, 8),
        color: "#78e2c1",
        fillColor: "#183b3b",
        fillOpacity: 0.9,
        weight: 2,
      })
        .addTo(map)
        .bindPopup(popup)
        .bindTooltip(`${facility.city}: ${items.length} cases`, {
          permanent: true,
          direction: "auto",
          offset: [0, 0],
          className: "facility-city-label",
        });
      marker.getElement()?.setAttribute("data-facility-id", facility.id);
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
      <p className="map-caption">
        Bundled Natural Earth country outlines, public domain and generalized
        at 1:110m. Pan and zoom without an external tile service. Markers show
        synthetic operations desks in Athens, Thessaloniki and Patras, with
        current-session case counts. They do not represent hospitals, patient
        locations or live care coverage.
      </p>
    </>
  );
}
