// Jedyne miejsce importu z wygenerowanego api-types.ts (HAR-84): rozjazd kontraktu = blad tsc.
import type { components } from "./api-types";

type Schemat = components["schemas"];

export type RunSzczegoly = Schemat["RunSzczegoly"];
export type ZdarzenieRunu = Schemat["ZdarzenieRunu"];
export type RunUtworzony = Schemat["RunUtworzony"];
export type RunnerZespolu = Schemat["RunnerZespolu"];
export type Katalog = Schemat["Katalog"];
export type DefinicjaKatalogu = Schemat["DefinicjaKatalogu"];
export type KrokRunu = Schemat["KrokRunu"];
export type RunStatus = Schemat["RunStatus"];
export type Profil = Schemat["Profil"];
