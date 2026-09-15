// ---------------------------------------------------------------------------
// recovered_speck -- the RTL as reconstructed from the netlist alone.
//
// Written after steps 1-7 of guide.html and before design.v was looked at
// again.  Every line carries the observation it came from; nothing here is a
// guess dressed up as a fact, and where the netlist could not say, the comment
// says so.
//
// The names are the ones Quartus happened to keep in the export.  They are
// *not* evidence -- a hostile flow drops them, and walkthrough 16 is the one
// that does the same job with them gone.  What is evidence is written next to
// each line.
// ---------------------------------------------------------------------------

`default_nettype none

module recovered_speck (
    input  wire        clk,      // the only net reaching all 104 tennm_ff .clk pins
    input  wire        rst_n,    // the only net reaching all 104 .clrn pins; the
                                 // tennm_ff model makes clrn ASYNCHRONOUS and
                                 // ACTIVE LOW, so this is an async clear to 0
    input  wire        start,    // reaches no control pin; it selects the load
                                 // path of every word register's next-state cell
    input  wire [31:0] pt,       // reaches only the load side of the x/y cells
    input  wire [63:0] key,      // reaches only the load side of the k/l cells
    output wire [31:0] ct,       // driven straight by the x and y register outputs
    output wire        busy,     // driven by the flag that gates the counter's enable
    output wire        done      // driven by the flag the counter's last value sets
);

    // ---- recovered constants ----------------------------------------------
    // ROUNDS: the round counter's observed range while busy is 0..21 and the
    //         netlist takes 23 clock edges from an accepted start to done
    //         (step 6), so 22 rounds after one load cycle.
    // ALPHA:  slice i of the data-path carry chain reads x[(i+7) mod 16]
    //         (step 3) and slice i of the key-schedule chain reads
    //         l0[(i+7) mod 16].  Both chains, the same 7.
    // BETA:   bit i of the y bank's next-state cell reads y[(i-2) mod 16]
    //         (step 4), and bit i of the k bank's reads k[(i-2) mod 16].
    localparam integer ROUNDS = 22;
    localparam integer ALPHA  = 7;
    localparam integer BETA   = 2;

    // ---- state -------------------------------------------------------------
    // Six 16-bit q vectors (step 2) on one clock and one clear, in two enable
    // groups: 96 bits share one enable, 5 more share another, 3 have none.
    reg [15:0] x, y;              // the two words the data-path chain adds
    reg [15:0] k;                 // the word both the chain and the XOR layer read
    reg [15:0] l0, l1, l2;        // three banks wired q->d in a line into l0
    reg [4:0]  rnd;               // the 5-bit counter, enabled by `run`
    reg        run, pen, fin;     // the three unenabled flags

    wire load = start & ~run;     // the cofactor that recovered the XOR layer:
                                  // 53 of the 54 XOR cells are XORs exactly when
                                  // run = 1, i.e. when the load path is not taken

    // ---- the round function (steps 3, 4, 5) --------------------------------
    // One verified 16-bit carry chain, one rotation in its operand order, one
    // XOR layer on its sums; then a second rotation and a second XOR into y.
    wire [15:0] x_rot = {x[ALPHA-1:0], x[15:ALPHA]};
    wire [15:0] x_nxt = (x_rot + y) ^ k;
    wire [15:0] y_rot = {y[15-BETA:0], y[15:16-BETA]};
    wire [15:0] y_nxt = y_rot ^ x_nxt;

    // ---- the key schedule: the same shape, index instead of key ------------
    // The second chain has the identical structure. Its XOR layer reads the
    // round counter on five bits and nothing on the other eleven -- which is
    // what says the injected word is the round index and not a key.
    wire [15:0] l_rot = {l0[ALPHA-1:0], l0[15:ALPHA]};
    wire [15:0] l_nxt = (l_rot + k) ^ {11'b0, rnd};
    wire [15:0] k_rot = {k[15-BETA:0], k[15:16-BETA]};
    wire [15:0] k_nxt = k_rot ^ l_nxt;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            x   <= 16'h0000;
            y   <= 16'h0000;
            k   <= 16'h0000;
            l0  <= 16'h0000;
            l1  <= 16'h0000;
            l2  <= 16'h0000;
            rnd <= 5'd0;
            run <= 1'b0;
            pen <= 1'b0;
            fin <= 1'b0;
        end else begin
            if (load) begin
                // which port bit reaches which bank's load input (step 2)
                x  <= pt[31:16];
                y  <= pt[15:0];
                k  <= key[15:0];
                l0 <= key[31:16];
                l1 <= key[47:32];
                l2 <= key[63:48];
            end else if (run) begin
                x  <= x_nxt;
                y  <= y_nxt;
                k  <= k_nxt;
                l0 <= l1;          // the l banks are wired q->d in a line
                l1 <= l2;
                l2 <= l_nxt;
            end

            if (run) begin
                rnd <= pen ? 5'd0 : rnd + 5'd1;
            end

            pen <= run & (rnd == ROUNDS - 2);
            if (load) begin
                run <= 1'b1;
                fin <= 1'b0;
            end else if (run & pen) begin
                run <= 1'b0;
                fin <= 1'b1;
            end
        end
    end

    assign ct   = {x, y};
    assign busy = run;
    assign done = fin;

endmodule

`default_nettype wire
