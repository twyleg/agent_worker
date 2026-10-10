# Source this file in Fish: source /path/to/agent_worker_vm/activate.fish
begin
    set -l activation_dir (path dirname -- (status filename))
    set -l scripts_dir (path resolve -- "$activation_dir/scripts")
    if not contains -- "$scripts_dir" $PATH
        set -gx PATH "$scripts_dir" $PATH
    end
end
