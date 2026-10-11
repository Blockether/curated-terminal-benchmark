;; Drives one samizdat run for the curated benchmark: wait for the local
;; server, start the run, poll it to a terminal status, save the detail.
(ns ctb.drive
  (:require [jolt.time]
            [clojure.data.json :as json]
            [samizdat.api.client :as client]))

(def terminal #{"completed" "aborted" "failed" "interrupted" "exhausted" "abandoned"})

(defn- env-long [k default]
  (or (some-> (System/getenv k) parse-long) default))

(defn- wait-server [base]
  (loop [n 0]
    (let [r (client/list-runs base)]
      (cond (:ok r) true
            (> n 300) (do (println "server did not start:" (:error r)) false)
            :else (do (Thread/sleep 1000) (recur (inc n)))))))

(defn- poll [base run-id out]
  (loop []
    (Thread/sleep 10000)
    (let [r (client/run-detail base run-id)
          status (get-in r [:body :run :status])]
      (when (:ok r) (spit out (json/write-str (:body r))))
      (if (contains? terminal status)
        status
        (recur)))))

(defn -main [& _]
  (let [base (str "http://127.0.0.1:" (env-long "HARNESS_PORT" 3985))
        out "/logs/agent/samizdat-run.json"]
    (if-not (wait-server base)
      (System/exit 2)
      (let [r (client/start-run! base (cond-> {:problem (slurp (System/getenv "CTB_PROBLEM_FILE"))
                                               :beam_width (env-long "CTB_BEAM_WIDTH" 1)}
                                        (System/getenv "CTB_MAX_TURNS")
                                        (assoc :max_turns (env-long "CTB_MAX_TURNS" nil))))
            run-id (get-in r [:body :run_id])]
        (if-not (and (:ok r) run-id)
          (do (println "run did not start:" (:error r)) (System/exit 3))
          (let [status (poll base run-id out)]
            (println "run" run-id status)
            ;; The verifier sets the reward; the run status goes to the trial metadata.
            (System/exit 0)))))))
