// ---------------------------------------------------------------------------
// recovered.v -- behavioural model of the Jane Street 2026 "ASIC reverse
// engineering" puzzle chip, recovered from puzzle.gds by structural analysis
// of the extracted gate netlist (no SAT solver involved).
//
// The chip is a Star Battle judge.  121 serial bits are an 11x11 grid read
// row-major (11 "characters" of 11 bits); a 1 is a star.  It raises `success`
// iff the grid is a valid Star Battle solution for the region map that is
// hard-wired into ~130 gates of random logic:
//
//   * exactly 2 stars per row, per column and per region
//   * 22 stars in total
//   * no two stars orthogonally or diagonally adjacent
//
// While the grid streams in, an 8-bit LFSR seeded with 0xA5 absorbs every
// payload bit.  On acceptance the answer is emitted as that LFSR XORed with a
// fixed per-byte mask, which is why the success text is not visible as a
// constant anywhere in the layout: you have to solve the puzzle first.
//
// Interface, reset behaviour and cycle timing are bit-exact against the
// gate-level simulation of artifacts/puzzle_netlist.json -- see
// tools/check_recovered.py (233/233 stimuli match, O[7:0] and success compared
// on every rising edge).  tools/reference.py is the Python twin of this file.
//
// Protocol
//   rst_n low for >= 1 rising edge (async, active low)
//   enable = 1 for exactly 121 rising edges, one payload bit on I each
//   enable = 0; O[7:0] then emits one byte per rising edge, 15 bytes, NUL pad
// ---------------------------------------------------------------------------

`timescale 1ns / 1ps

module puzzle (
    input  wire       clk,
    input  wire       rst_n,     // asynchronous, active low
    input  wire       enable,
    input  wire       I,
    output reg  [7:0] O,
    output reg        success
);

    // ---- state -----------------------------------------------------------
    reg  [3:0]  col;             // bit index inside the character  (0..10)
    reg  [3:0]  row;             // character index                 (0..10)
    reg         done;            // all 121 bits consumed
    reg         outphase;        // message is being emitted
    reg  [11:0] sr;              // input delay line, sr[0] = 1 cycle ago
    reg         adj_bad;         // two stars touched
    reg  [7:0]  pop;             // total number of stars seen
    reg         row_prev;        // per-row star count, low bit
    reg         row_two;         // per-row star count, high bit
    reg         row_bad;         // some row did not have exactly 2 stars
    reg  [1:0]  col_cnt [0:10];  // per-column saturating star counters
    reg  [1:0]  reg_cnt [0:10];  // per-region saturating star counters
    reg  [7:0]  lfsr;            // message key / input digest
    reg  [3:0]  outcnt;          // output byte counter, freezes at 15
    reg         near;            // everything right except the touching rule

    integer     i;

    // ---- the region map, read straight out of the gate netlist ------------
    function [3:0] region;
        input [3:0] r;
        input [3:0] c;
        reg [43:0] rw;
        begin
            case (r)
              4'd0 : rw = 44'h94458866666;  // 6 6 6 6 6 8 8 5 4 4 9
              4'd1 : rw = 44'h94455866066;  // 6 6 0 6 6 8 5 5 4 4 9
              4'd2 : rw = 44'h94558888066;  // 6 6 0 8 8 8 8 5 5 4 9
              4'd3 : rw = 44'h95591118066;  // 6 6 0 8 1 1 1 9 5 5 9
              4'd4 : rw = 44'h99999918060;  // 0 6 0 8 1 9 9 9 9 9 9
              4'd5 : rw = 44'h22291118000;  // 0 0 0 8 1 1 1 9 2 2 2
              4'd6 : rw = 44'hAA291888888;  // 8 8 8 8 8 8 1 9 2 A A
              4'd7 : rw = 44'hAA291117778;  // 8 7 7 7 1 1 1 9 2 A A
              4'd8 : rw = 44'hAA299993778;  // 8 7 7 3 9 9 9 9 2 A A
              4'd9 : rw = 44'h22299933788;  // 8 8 7 3 3 9 9 9 2 2 2
              default: rw = 44'h99999993778; // 8 7 7 3 9 9 9 9 9 9 9
            endcase
            region = rw[4*c +: 4];
        end
    endfunction

    // ---- canned messages (index = outcnt) ---------------------------------
    function [7:0] msg_fail;   input [3:0] k; begin
        case (k) 0:msg_fail="T"; 1:msg_fail="R"; 2:msg_fail="Y"; 3:msg_fail=" ";
                 4:msg_fail="A"; 5:msg_fail="G"; 6:msg_fail="A"; 7:msg_fail="I";
                 8:msg_fail="N"; default: msg_fail=8'h00; endcase end
    endfunction
    // NOTE: byte 3 really is 0x22 in the silicon -- almost certainly a typo
    // for a space in the original RTL, reproduced here for bit-exactness.
    function [7:0] msg_touch;  input [3:0] k; begin
        case (k) 0:msg_touch="T"; 1:msg_touch="W"; 2:msg_touch="O"; 3:msg_touch=8'h22;
                 4:msg_touch="N"; 5:msg_touch="O"; 6:msg_touch="T"; 7:msg_touch=" ";
                 8:msg_touch="T"; 9:msg_touch="O"; 10:msg_touch="U"; 11:msg_touch="C";
                 12:msg_touch="H"; default: msg_touch=8'h00; endcase end
    endfunction
    function [7:0] msg_empty;  input [3:0] k; begin
        case (k) 0:msg_empty="E"; 1:msg_empty="M"; 2:msg_empty="P"; 3:msg_empty="T";
                 4:msg_empty="Y"; 5:msg_empty=" "; 6:msg_empty="S"; 7:msg_empty="K";
                 8:msg_empty="Y"; default: msg_empty=8'h00; endcase end
    endfunction
    function [7:0] msg_full;   input [3:0] k; begin
        case (k) 0:msg_full="B"; 1:msg_full="I"; 2:msg_full="G"; 3:msg_full=" ";
                 4:msg_full="B"; 5:msg_full="A"; 6:msg_full="N"; 7:msg_full="G";
                 default: msg_full=8'h00; endcase end
    endfunction
    // success text = lfsr ^ mask, so it is not a constant in the layout
    function [7:0] msg_mask;   input [3:0] k; begin
        case (k) 0:msg_mask=8'h4d; 1:msg_mask=8'had; 2:msg_mask=8'hfb; 3:msg_mask=8'h83;
                 4:msg_mask=8'h13; 5:msg_mask=8'h79; 6:msg_mask=8'h1c; 7:msg_mask=8'hb5;
                 8:msg_mask=8'h79; 9:msg_mask=8'h63; 10:msg_mask=8'hc7; 11:msg_mask=8'h68;
                 12:msg_mask=8'h93; 13:msg_mask=8'hf5; 14:msg_mask=8'h8f;
                 default: msg_mask=8'h00; endcase end
    endfunction

    // ---- the LFSR ---------------------------------------------------------
    function [7:0] lfsr_step;            // one step, payload bit mixed in
        input [7:0] s;
        input       b;
        begin
            lfsr_step = {s[6:0], b ^ s[7] ^ s[5] ^ s[4] ^ s[3]};
        end
    endfunction

    function [7:0] lfsr_x8;              // eight free-running steps
        input [7:0] s;
        integer j;
        begin
            lfsr_x8 = s;
            for (j = 0; j < 8; j = j + 1) lfsr_x8 = lfsr_step(lfsr_x8, 1'b0);
        end
    endfunction

    // ---- combinational helpers -------------------------------------------
    wire       act      = enable & ~done;          // n192
    wire       last_col = (col == 4'd10);          // n292
    wire       last_row = (row == 4'd10);
    wire [3:0] sel      = region(row, col);
    wire       step_out = outphase & (outcnt != 4'd15) & ~act;

    // the n418 touching check: the current star against the four already-seen
    // neighbours held in the 12-deep delay line
    wire touch = I & act & (((col != 4'd0)  & sr[11]) |   // up-left
                            ((col != 4'd0)  & sr[0])  |   // left
                            ((col != 4'd10) & sr[9])  |   // up-right
                                              sr[10]);    // up
    // the n284 per-row check, evaluated on the last bit of each character
    wire row_now_bad = (~(I & row_prev) & ~row_two) | (row_two & (I | row_prev));

    // all of the counting constraints (n703 & n713 minus the touching term)
    reg counts_ok;
    always @* begin
        counts_ok = (pop == 8'd22) & ~row_bad & done & ~outphase;
        for (i = 0; i < 11; i = i + 1) begin
            if (col_cnt[i] != 2'd2) counts_ok = 1'b0;
            if (reg_cnt[i] != 2'd2) counts_ok = 1'b0;
        end
    end

    // ---- output ROM / keystream ------------------------------------------
    always @* begin
        if (!outphase || outcnt == 4'd15)      O = 8'h00;
        else if (pop == 8'd0)                  O = msg_empty(outcnt);
        else if (pop == 8'd121)                O = msg_full(outcnt);
        else if (success)                      O = lfsr ^ msg_mask(outcnt);
        else if (near)                         O = msg_touch(outcnt);
        else                                   O = msg_fail(outcnt);
    end

    // ---- sequential -------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            col      <= 4'd0;
            row      <= 4'd0;
            done     <= 1'b0;
            outphase <= 1'b0;
            sr       <= 12'd0;
            adj_bad  <= 1'b0;
            pop      <= 8'd0;
            row_prev <= 1'b0;
            row_two  <= 1'b0;
            row_bad  <= 1'b0;
            lfsr     <= 8'hA5;               // the four dfstp_2 cells
            outcnt   <= 4'd0;
            success  <= 1'b0;
            near     <= 1'b0;
            for (i = 0; i < 11; i = i + 1) begin
                col_cnt[i] <= 2'd0;
                reg_cnt[i] <= 2'd0;
            end
        end else begin
            // verdict: latched on the single cycle where done=1 and outphase=0
            if (done & ~outphase) begin
                success <= counts_ok & ~adj_bad;
                near    <= counts_ok &  adj_bad;
            end

            if (touch)                    adj_bad <= 1'b1;
            if (act & last_col & row_now_bad) row_bad <= 1'b1;

            if (act) begin
                sr   <= {sr[10:0], I};
                pop  <= pop + I;
                lfsr <= lfsr_step(lfsr, I);
                if (I) begin
                    if (col_cnt[col] != 2'd3) col_cnt[col] <= col_cnt[col] + 2'd1;
                    if (reg_cnt[sel] != 2'd3) reg_cnt[sel] <= reg_cnt[sel] + 2'd1;
                end
                if (last_col) begin
                    col      <= 4'd0;
                    row_prev <= 1'b0;
                    row_two  <= 1'b0;
                    row      <= last_row ? 4'd0 : row + 4'd1;
                    if (last_row) done <= 1'b1;
                end else begin
                    col      <= col + 4'd1;
                    row_two  <= row_two | (I & row_prev);
                    row_prev <= (I | row_prev) & (~(I & row_prev) | row_two);
                end
            end else if (step_out) begin
                // one keystream byte per emitted byte: eight LFSR steps.
                // (The gates implement the collapsed 8-step matrix; this is
                //  bit-identical for all 256 states.)
                lfsr <= lfsr_x8(lfsr);
            end

            if (outphase & (outcnt != 4'd15)) outcnt <= outcnt + 4'd1;
            if (done)                         outphase <= 1'b1;
        end
    end

endmodule
