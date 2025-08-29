import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { authApiClient } from "../../lib/api-client";
import { toast } from "sonner";

interface MediaServerConfig {
  triggerOnQueueEmpty: boolean;
  intervalEnabled: boolean;
  intervalSeconds: number;
  jellyfin: { enabled: boolean; url: string; apiKey: string };
  plex: { enabled: boolean; url: string; apiKey: string; librarySectionIds: string };
}

const fetchMediaServers = async (): Promise<MediaServerConfig> => {
  const { data } = await authApiClient.client.get("/config/media-servers");
  return data;
};

const saveMediaServers = async (data: Partial<MediaServerConfig>) => {
  const { data: response } = await authApiClient.client.put("/config/media-servers", data);
  return response;
};

const triggerScan = async () => {
  const { data } = await authApiClient.client.post("/config/media-servers/scan", {});
  return data;
};

export function MediaServersTab() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["mediaServers"], queryFn: fetchMediaServers });
  const { register, handleSubmit, watch, reset } = useForm<MediaServerConfig>();

  useEffect(() => { if (data) reset(data); }, [data, reset]);

  const mutation = useMutation({
    mutationFn: saveMediaServers,
    onSuccess: () => {
      toast.success("Media server settings saved");
      queryClient.invalidateQueries({ queryKey: ["mediaServers"] });
    },  
    onError: (e: any) => toast.error(e?.response?.data?.detail || e.message || "Save failed"),
  });

  const scanMutation = useMutation({
    mutationFn: triggerScan,
    onSuccess: () => toast.success("Scan triggered"),
    onError: (e: any) => toast.error(e?.response?.data?.detail || e.message || "Scan failed"),
  });

  const jellyfinEnabled = watch("jellyfin.enabled");
  const plexEnabled = watch("plex.enabled");
  const intervalEnabled = watch("intervalEnabled");

  const onSubmit = (form: MediaServerConfig) => {
    // Basic client-side validation
    if (intervalEnabled && form.intervalSeconds < 60) {
      toast.error("Interval must be at least 60 seconds");
      return;
    }
    if (jellyfinEnabled && !form.jellyfin.url) {
      toast.error("Jellyfin URL required when enabled");
      return;
    }
    if (jellyfinEnabled && !form.jellyfin.apiKey) {
      toast.error("Jellyfin API Key required when enabled");
      return;
    }
    if (plexEnabled && !form.plex.url) {
      toast.error("Plex URL required when enabled");
      return;
    }
    if (plexEnabled && !form.plex.apiKey) {
      toast.error("Plex Token required when enabled");
      return;
    }
    mutation.mutate({
      ...form,
      intervalSeconds: Number(form.intervalSeconds),
      jellyfin: { ...form.jellyfin },
      plex: { ...form.plex },
    });
  };

  if (isLoading) return <p className="text-content-muted dark:text-content-muted-dark">Loading media server settings...</p>;

  return (
    <form onSubmit={handleSubmit(onSubmit)} className="space-y-10">
      <div className="flex items-center justify-end gap-3">
        <button
          type="button"
          onClick={() => scanMutation.mutate()}
          disabled={scanMutation.isPending}
          className="px-4 py-2 bg-button-secondary hover:bg-button-secondary-hover text-button-secondary-text hover:text-button-secondary-text-hover rounded-md disabled:opacity-50"
        >
          {scanMutation.isPending ? "Scanning..." : "Trigger Scan"}
        </button>
        <button
          type="submit"
          disabled={mutation.isPending}
          className="px-4 py-2 bg-button-primary hover:bg-button-primary-hover text-button-primary-text rounded-md disabled:opacity-50"
        >
          {mutation.isPending ? "Saving..." : "Save Media Servers"}
        </button>
      </div>

      <section className="space-y-4">
        <h3 className="text-xl font-semibold text-content-primary dark:text-content-primary-dark">Global Scan Triggers</h3>
        <div className="flex items-center justify-between">
          <label className="text-content-primary dark:text-content-primary-dark" htmlFor="triggerOnQueueEmpty">Trigger When Queue Empty</label>
          <input id="triggerOnQueueEmpty" type="checkbox" {...register("triggerOnQueueEmpty")} className="h-6 w-6" />
        </div>
        <div className="flex items-center justify-between">
          <label className="text-content-primary dark:text-content-primary-dark" htmlFor="intervalEnabled">Enable Interval Scan</label>
          <p className="text-xs text-content-muted dark:text-content-muted-dark">Consider using the built-in functions in jellyfin/plex</p>
          <input id="intervalEnabled" type="checkbox" {...register("intervalEnabled")} className="h-6 w-6" />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor="intervalSeconds" className="text-content-primary dark:text-content-primary-dark">Interval (seconds)</label>
          <input
            id="intervalSeconds"
            type="number"
            min={60}
            disabled={!intervalEnabled}
            {...register("intervalSeconds", { valueAsNumber: true })}
            className="block w-full p-2 border bg-input-background dark:bg-input-background-dark border-input-border dark:border-input-border-dark rounded-md focus:outline-none focus:ring-2 focus:ring-input-focus disabled:opacity-50"
          />
          <p className="text-xs text-content-muted dark:text-content-muted-dark">Minimum 60 seconds.</p>
        </div>
      </section>

      <section className="space-y-4">
        <h3 className="text-xl font-semibold text-content-primary dark:text-content-primary-dark">Jellyfin</h3>
        <div className="flex items-center justify-between">
          <label className="text-content-primary dark:text-content-primary-dark" htmlFor="jellyfinEnabled">Enable Jellyfin</label>
          <input id="jellyfinEnabled" type="checkbox" {...register("jellyfin.enabled")} className="h-6 w-6" />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor="jellyfinUrl" className="text-content-primary dark:text-content-primary-dark">URL</label>
          <input
            id="jellyfinUrl"
            type="text"
            placeholder="http://jellyfin:8096"
            disabled={!jellyfinEnabled}
            {...register("jellyfin.url")}
            className="block w-full p-2 border bg-input-background dark:bg-input-background-dark border-input-border dark:border-input-border-dark rounded-md focus:outline-none focus:ring-2 focus:ring-input-focus disabled:opacity-50"
          />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor="jellyfinApiKey" className="text-content-primary dark:text-content-primary-dark">API Key</label>
            <input
              id="jellyfinApiKey"
              type="password"
              placeholder="••••••"
              disabled={!jellyfinEnabled}
              {...register("jellyfin.apiKey")}
              className="block w-full p-2 border bg-input-background dark:bg-input-background-dark border-input-border dark:border-input-border-dark rounded-md focus:outline-none focus:ring-2 focus:ring-input-focus disabled:opacity-50"
            />
        </div>
      </section>

      <section className="space-y-4">
        <h3 className="text-xl font-semibold text-content-primary dark:text-content-primary-dark">Plex</h3>
        <div className="flex items-center justify-between">
          <label className="text-content-primary dark:text-content-primary-dark" htmlFor="plexEnabled">Enable Plex</label>
          <input id="plexEnabled" type="checkbox" {...register("plex.enabled")} className="h-6 w-6" />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor="plexUrl" className="text-content-primary dark:text-content-primary-dark">URL</label>
          <input
            id="plexUrl"
            type="text"
            placeholder="http://plex:32400"
            disabled={!plexEnabled}
            {...register("plex.url")}
            className="block w-full p-2 border bg-input-background dark:bg-input-background-dark border-input-border dark:border-input-border-dark rounded-md focus:outline-none focus:ring-2 focus:ring-input-focus disabled:opacity-50"
          />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor="plexApiKey" className="text-content-primary dark:text-content-primary-dark">Token</label>
          <input
            id="plexApiKey"
            type="password"
            placeholder="••••••"
            disabled={!plexEnabled}
            {...register("plex.apiKey")}
            className="block w-full p-2 border bg-input-background dark:bg-input-background-dark border-input-border dark:border-input-border-dark rounded-md focus:outline-none focus:ring-2 focus:ring-input-focus disabled:opacity-50"
          />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor="plexSectionIds" className="text-content-primary dark:text-content-primary-dark">Library Section IDs (comma separated)</label>
          <input
            id="plexSectionIds"
            type="text"
            placeholder="1,2"
            disabled={!plexEnabled}
            {...register("plex.librarySectionIds")}
            className="block w-full p-2 border bg-input-background dark:bg-input-background-dark border-input-border dark:border-input-border-dark rounded-md focus:outline-none focus:ring-2 focus:ring-input-focus disabled:opacity-50"
          />
          <p className="text-xs text-content-muted dark:text-content-muted-dark">Leave empty to refresh all sections.</p>
        </div>
      </section>
    </form>
  );
}
